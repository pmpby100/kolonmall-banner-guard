# Use the official Python base image
FROM python:3.11-slim

# Set environment variables for Hugging Face Spaces
ENV PYTHONUNBUFFERED=1 \
    PORT=7860 \
    HOME=/home/user \
    PATH=/home/user/.local/bin:$PATH

WORKDIR /code

# Create user with UID 1000 for Hugging Face Spaces (required)
RUN useradd -m -u 1000 user

# Copy requirements and install
COPY backend/requirements.txt /code/requirements.txt
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r /code/requirements.txt

# Install Playwright and its dependencies
RUN playwright install chromium && \
    playwright install-deps chromium

# Copy backend code and give ownership to the user
COPY --chown=user:user backend/ /code/backend/

# Switch to the non-root user
USER user

# Set working directory to backend
WORKDIR /code/backend

# Create screenshots directory to prevent permission errors
RUN mkdir -p screenshots

EXPOSE 7860

# Command to run the FastAPI app on port 7860
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "7860"]
