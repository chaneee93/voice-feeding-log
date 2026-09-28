# 파이썬 3.12가 깔린 가벼운 리눅스에서 시작
FROM python:3.12-slim

# 컨테이너 안 작업 폴더
WORKDIR /app

# 라이브러리 먼저 설치 (코드만 바뀌면 이 단계는 캐시로 건너뜀)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 우리 코드 복사
COPY main.py .

# 기록 파일은 컨테이너 밖(볼륨)에 저장
ENV DB_PATH=/data/feeding.db
EXPOSE 8000

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
