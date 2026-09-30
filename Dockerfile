FROM mcr.microsoft.com/playwright/python:v1.44.0-jammy

# إعطاء صلاحيات كاملة لمجلد المتصفحات
RUN chmod -R 777 /ms-playwright

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# إعداد المستخدم 1000 لـ Hugging Face
RUN useradd -m -u 1000 user && chown -R user:user /app
USER user

EXPOSE 7860

CMD ["python", "server.py"]
