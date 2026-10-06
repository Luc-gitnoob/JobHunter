# Dockerfile for JobHunter
FROM python:3.12-slim

# Prevent Python from writing .pyc and buffer stdout/stderr
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Install system dependencies + TeX Live for PDF resume generation
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    curl \
    texlive-latex-base \
    texlive-latex-extra \
    texlive-fonts-recommended \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# Copy source code and configuration
COPY . .

# Ensure directories exist
RUN mkdir -p data output/resumes

# Expose web dashboard port
EXPOSE 8080

# Default command runs full system (poller scheduler + web dashboard)
CMD ["python", "-m", "src.main"]
