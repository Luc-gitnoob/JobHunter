"""LaTeX → PDF resume compiler with Jinja2 templating."""

import logging
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from jinja2 import Environment, FileSystemLoader

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
TEMPLATE_DIR = Path(__file__).resolve().parent
TEMPLATE_FILE = "template.tex"
OUTPUT_DIR = PROJECT_ROOT / "output" / "resumes"


def _escape_latex(text: str) -> str:
    """Escape special LaTeX characters in text content."""
    if not text:
        return ""
    # Characters that need escaping in LaTeX
    special_chars = {
        "&": r"\&",
        "%": r"\%",
        "$": r"\$",
        "#": r"\#",
        "_": r"\_",
        "~": r"\textasciitilde{}",
        "^": r"\textasciicircum{}",
    }
    # Don't escape backslashes that are already part of LaTeX commands
    for char, escaped in special_chars.items():
        text = text.replace(char, escaped)
    return text


def _escape_context(obj):
    """Recursively escape all string values in a context dict/list."""
    if isinstance(obj, str):
        return _escape_latex(obj)
    elif isinstance(obj, dict):
        return {k: _escape_context(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [_escape_context(item) for item in obj]
    return obj


class ResumeCompiler:
    """
    Compiles a LaTeX resume from template + context data.

    Uses Jinja2 with custom delimiters to avoid conflicts with LaTeX:
    - Variable: ((( variable )))
    - Block:    ((* block *))
    - Comment:  ((# comment #))
    """

    def __init__(self, config: dict = None):
        config = config or {}
        self.latex_compiler = config.get("latex_compiler", "pdflatex")
        self.output_dir = Path(config.get("output_dir", OUTPUT_DIR))
        self.output_dir.mkdir(parents=True, exist_ok=True)

        # Set up Jinja2 with LaTeX-safe delimiters
        self.env = Environment(
            loader=FileSystemLoader(str(TEMPLATE_DIR)),
            variable_start_string="(((",
            variable_end_string=")))",
            block_start_string="((*",
            block_end_string="*))",
            comment_start_string="((#",
            comment_end_string="#))",
            autoescape=False,  # We handle escaping ourselves
        )

    def compile(self, context: dict, output_filename: str) -> Path:
        """
        Render the LaTeX template with the given context and compile to PDF.

        Args:
            context: Template variables (from ResumeTailor.build_template_context)
            output_filename: Name for the output PDF (without extension)

        Returns:
            Path to the generated PDF file (or .tex source file if compiler is not installed)
        """
        # Escape all string content for LaTeX safety
        escaped_context = _escape_context(context)

        # Render the template
        template = self.env.get_template(TEMPLATE_FILE)
        rendered_tex = template.render(**escaped_context)

        # Sanitize filename and save .tex source permanently
        safe_name = re.sub(r"[^\w\-.]", "_", output_filename)
        tex_dest = self.output_dir / f"{safe_name}.tex"
        tex_dest.write_text(rendered_tex, encoding="utf-8")
        logger.info(f"[Compiler] Saved LaTeX resume source: {tex_dest}")

        # Check if LaTeX compiler is installed on the system
        if not self.check_latex_installed():
            logger.warning(
                f"[Compiler] LaTeX compiler '{self.latex_compiler}' not found on system. "
                f"Saved tailored LaTeX resume source at: {tex_dest}"
            )
            return tex_dest

        # Write to a temp directory and compile
        with tempfile.TemporaryDirectory() as tmpdir:
            tex_path = Path(tmpdir) / "resume.tex"
            tex_path.write_text(rendered_tex, encoding="utf-8")

            # Run pdflatex twice (for references to resolve)
            for pass_num in range(2):
                try:
                    result = subprocess.run(
                        [
                            self.latex_compiler,
                            "-interaction=nonstopmode",
                            "-output-directory", tmpdir,
                            str(tex_path),
                        ],
                        capture_output=True,
                        text=True,
                        timeout=30,
                        cwd=tmpdir,
                    )
                    if result.returncode != 0 and pass_num == 1:
                        logger.error(
                            f"LaTeX compilation failed (pass {pass_num + 1}):\n"
                            f"STDOUT: {result.stdout[-1000:]}\n"
                            f"STDERR: {result.stderr[-1000:]}"
                        )
                        return tex_dest
                except FileNotFoundError:
                    return tex_dest

            # Copy the PDF to output directory
            pdf_source = Path(tmpdir) / "resume.pdf"
            if not pdf_source.exists():
                logger.warning(f"PDF was not generated. Retaining .tex file: {tex_dest}")
                return tex_dest

            pdf_dest = self.output_dir / f"{safe_name}.pdf"
            shutil.copy2(str(pdf_source), str(pdf_dest))

            logger.info(f"[Compiler] Generated resume PDF: {pdf_dest}")
            return pdf_dest

    def check_latex_installed(self) -> bool:
        """Check if the LaTeX compiler is available on the system."""
        try:
            result = subprocess.run(
                [self.latex_compiler, "--version"],
                capture_output=True,
                timeout=10,
            )
            return result.returncode == 0
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return False
