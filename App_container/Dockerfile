FROM python:3.11-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Tell Gradio to use a fixed, known temp directory so that files
# we write there (overlays, masks, CSVs, ZIPs) are within Gradio's
# allowed serving zone and the /file= route returns 200, not 404.
ENV GRADIO_TEMP_DIR=/tmp/gradio

COPY app.py .
COPY templates/ templates/

EXPOSE 8001
CMD ["python", "app.py"]
