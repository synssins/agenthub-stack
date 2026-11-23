# Chatterbox Turbo streaming server (OpenAI + Wyoming). Needs the NVIDIA container runtime.
FROM python:3.12
# Clean upstream at the reviewed base commit, then the reviewed/modified files from ./src on top.
RUN git clone https://github.com/resemble-ai/chatterbox /src \
 && git -C /src checkout 5de7a54aa4e5e2baadb0182dde554908b48b85c2
COPY src/ /src/src/
RUN pip install --no-cache-dir torch==2.6.0 torchaudio==2.6.0 --index-url https://download.pytorch.org/whl/cu124 \
 && pip install --no-cache-dir -e /src wyoming==1.11.0 fastapi==0.162.5 uvicorn==0.54.0
COPY server.py tags.py test_server.py test_tags.py ui.html /app/
ENV HF_HOME=/cache/hf
WORKDIR /app
CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "4125"]
