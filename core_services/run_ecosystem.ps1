# Install Dependencies
Write-Host "Installing pip dependencies..." -ForegroundColor Yellow
python -m pip install -r requirements.txt --quiet

# Launch Microservices in background with strict port locking to prevent overlap
Write-Host "Booting Titan Core Microservices on dedicated ports..." -ForegroundColor Cyan

Start-Process -NoNewWindow -FilePath "uvicorn" -ArgumentList "model_router:app", "--host", "127.0.0.1", "--port", "8001"
Start-Process -NoNewWindow -FilePath "uvicorn" -ArgumentList "policy_engine:app", "--host", "127.0.0.1", "--port", "8002"
Start-Process -NoNewWindow -FilePath "uvicorn" -ArgumentList "titansand_vm:app", "--host", "127.0.0.1", "--port", "8003"

Write-Host "=============================================" -ForegroundColor Green
Write-Host "SYSTEM ONLINE" -ForegroundColor Green
Write-Host "- Model Router: http://127.0.0.1:8001" -ForegroundColor Gray
Write-Host "- Policy Engine: http://127.0.0.1:8002" -ForegroundColor Gray
Write-Host "- TitanSand VM: http://127.0.0.1:8003" -ForegroundColor Gray
Write-Host "=============================================" -ForegroundColor Green
