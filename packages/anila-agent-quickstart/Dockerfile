# 這不是 MLSteam 開發者的啟動方式。
# lab 映像由 mlsteam/Dockerfile 建置；在 lab 裡用 ./run.sh。
# compose.yaml 只留給維護者本機看契約，不是 lab 入口。
FROM python:3.13-slim
WORKDIR /app
COPY agent.py server.py platform_io.py llm.py run.sh ./
EXPOSE 8200
CMD ["./run.sh", "status"]
