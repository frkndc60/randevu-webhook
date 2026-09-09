from fastapi import FastAPI, Request
import uvicorn
import os

app = FastAPI()

@app.get("/")
def home():
    return {"durum": "aktif", "mesaj": "Randevu Asistanı Webhook Sunucusu Çalışıyor!"}

@app.post("/api/vapi/webhook")
async def vapi_webhook(request: Request):
    data = await request.json()
    message = data.get("message", {})
    
    # Vapi yapay zekası randevu alma fonksiyonunu tetiklediğinde:
    if message.get("type") == "function-call":
        fn = message.get("functionCall", {})
        params = fn.get("parameters", {})
        
        musteri_adi = params.get("customerName", "Değerli Müşterimiz")
        tarih = params.get("appointmentTime", "Belirtilen saatte")
        hizmet = params.get("service", "Randevu")
        
        print(f"Yeni Randevu İsteği Geldi: {musteri_adi} - {tarih} - {hizmet}")
        
        return {
            "result": {
                "success": True,
                "message": f"Sayın {musteri_adi}, {tarih} tarihi için {hizmet} randevunuz başarıyla oluşturuldu."
            }
        }
        
    return {"status": "ok"}

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
