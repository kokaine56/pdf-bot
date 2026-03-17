# Use an official lightweight Python image
FROM python:3.10-slim

# Set working directory
WORKDIR /app

# Install system dependencies required for PDF tools
# poppler-utils: required by pdf2image
# tesseract-ocr: required by pytesseract
# pdfcrack: required for the brute-force cracking feature
# libmagic1: often useful for file type detection
RUN apt-get update && apt-get install -y \
    poppler-utils \
    tesseract-ocr \
    pdfcrack \
    libmagic1 \
    && rm -rf /var/lib/apt/lists/*

# Copy python dependencies
COPY requirements.txt .

# Install python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Copy the application code
COPY . .

# Set environment variables (Defaults can be overridden in docker run)
ENV PYTHONUNBUFFERED=1

# Run the bot
CMD ["python", "bot.py"]
