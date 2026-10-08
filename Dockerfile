FROM python:3.12.14-slim-bookworm
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 TESLA_DATA_DIR=/data
WORKDIR /app
RUN groupadd -g 10001 research && useradd -u 10001 -g research -m research
COPY requirements.lock pyproject.toml ./
RUN pip install --no-cache-dir --require-hashes -r requirements.lock
COPY src ./src
RUN pip install --no-cache-dir --no-deps .
COPY app.py ./
COPY .streamlit ./.streamlit
USER 10001:10001
EXPOSE 8501
CMD ["streamlit", "run", "app.py", "--server.address=0.0.0.0"]
