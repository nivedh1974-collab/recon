# Hugging Face Spaces (Docker SDK). Also runs anywhere Docker does:
#   docker build -t recon . && docker run -p 7860:7860 -e RECON_PUBLIC=1 recon
FROM python:3.12-slim

# Spaces run the container as user 1000
RUN useradd -m -u 1000 user
USER user
ENV HOME=/home/user \
    PATH=/home/user/.local/bin:$PATH \
    RECON_DB=/tmp/recon.db \
    PYTHONUNBUFFERED=1
WORKDIR /home/user/app

COPY --chown=user requirements.txt .
RUN pip install --no-cache-dir --user -r requirements.txt

COPY --chown=user . .

EXPOSE 7860
CMD ["streamlit", "run", "streamlit_app.py", "--server.port=7860", "--server.address=0.0.0.0", "--server.headless=true"]
