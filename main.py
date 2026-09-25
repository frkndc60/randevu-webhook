"""
SesAI Sunucusu
- Müşterinin SesAI uygulamasından girdiği bilgilerle Vapi'de asistan oluşturur / günceller
- Müşterinin kendi numarasını (SIP) Vapi'ye bağlar
- Vapi'den gelen randevu isteklerini Firestore'a kaydeder

Render > Settings > Environment kısmına eklenmesi gereken değişkenler:
  VAPI_PRIVATE_KEY            -> Vapi paneli > API Keys > Private Key
  VAPI_TEMPLATE_ASSISTANT_ID  -> Şablon asistan (şu an: 61a273b5-ef4e-47e5-864f-d07be7e855cf)
  FIREBASE_SERVICE_ACCOUNT    -> Firebase > Project settings > Service accounts > JSON dosyasının TAMAMI
"""

import json
import os
from datetime import datetime, timezone

import firebase_admin
import httpx
import uvicorn
from fastapi import FastAPI, Header, HTTPException, Request
from firebase_admin import auth as fb_auth
from firebase_admin import credentials, firestore
from pydantic import BaseModel

# ---------------------------------------------------------------------------
# Ayarlar
# ---------------------------------------------------------------------------
VAPI_BASE = "https://api.vapi.ai"
VAPI_PRIVATE_KEY = os.environ.get("VAPI_PRIVATE_KEY", "")
TEMPLATE_ASSISTANT_ID = os.environ.get(
    "VAPI_TEMPLATE_ASSISTANT_ID", "61a273b5-ef4e-47e5-864f-d07be7e855cf"
)

# Uygulamadaki 3 ses (ElevenLabs ses kimlikleri)
VOICES = {
    "yunus": {"voiceId": "Q5n6GDIjpN0pLOlycRFT", "name": "Yunus"},
    "nergis": {"voiceId": "IgiCa6883ksPGir0tfNK", "name": "Nergis"},
    "yagmur": {"voiceId": "IOx9E82IJLWAeUWBCdDz", "name": "Yağmur"},
}

# Şablon asistandan kopyalanacak ayarlar (model, transcriber, araçlar vb.)
TEMPLATE_KEYS = [
    "transcriber",
    "model",
    "voice",
    "server",
    "serverUrl",
    "serverMessages",
    "clientMessages",
    "endCallPhrases",
    "silenceTimeoutSeconds",
    "maxDurationSeconds",
    "backgroundSound",
    "backgroundDenoisingEnabled",
    "firstMessageMode",
    "startSpeakingPlan",
    "stopSpeakingPlan",
]

# ---------------------------------------------------------------------------
# Firebase
# ---------------------------------------------------------------------------
db = None
_sa = os.environ.get("FIREBASE_SERVICE_ACCOUNT", "")
if _sa:
    firebase_admin.initialize_app(credentials.Certificate(json.loads(_sa)))
    db = firestore.client()
else:
    print("UYARI: FIREBASE_SERVICE_ACCOUNT tanımlı değil, Firebase işlemleri çalışmayacak.")

app = FastAPI(title="SesAI Sunucusu")


# ---------------------------------------------------------------------------
# Yardımcılar
# ---------------------------------------------------------------------------
def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def require_setup():
    if db is None:
        raise HTTPException(500, "Sunucu yapılandırması eksik: FIREBASE_SERVICE_ACCOUNT")
    if not VAPI_PRIVATE_KEY:
        raise HTTPException(500, "Sunucu yapılandırması eksik: VAPI_PRIVATE_KEY")


def get_uid(authorization: str | None) -> str:
    """Uygulamadan gelen Firebase oturum anahtarını doğrular, kullanıcı kimliğini döner."""
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "Oturum bilgisi eksik")
    try:
        decoded = fb_auth.verify_id_token(authorization.split(" ", 1)[1])
        return decoded["uid"]
    except Exception:
        raise HTTPException(401, "Oturum geçersiz veya süresi dolmuş, lütfen tekrar giriş yapın")


async def vapi(method: str, path: str, body: dict | None = None) -> dict:
    headers = {"Authorization": f"Bearer {VAPI_PRIVATE_KEY}"}
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.request(method, f"{VAPI_BASE}{path}", headers=headers, json=body)
    if r.status_code >= 400:
        print(f"Vapi hatası {method} {path}: {r.status_code} {r.text}")
        raise HTTPException(502, f"Vapi hatası ({r.status_code}): {r.text[:300]}")
    return r.json() if r.text else {}


def build_system_prompt(d: "AssistantRequest", voice_name: str) -> str:
    return f"""Sen "{d.businessName}" işletmesinin telefon asistanısın. Adın {voice_name}.
Her zaman Türkçe, kibar, kısa ve doğal cümlelerle konuş.

İşletme bilgileri:
- İşletme adı: {d.businessName}
- Sektör: {d.sector or "Belirtilmedi"}
- Hizmetler: {d.services or "Belirtilmedi"}
- Çalışma saatleri: {d.workingHours or "Belirtilmedi"}
- Ek bilgiler: {d.extraInfo or "Yok"}

Konuşma kuralları (ÇOK ÖNEMLİ, yazdığın her şey sesli okunacak):
- Tüm sayıları, saatleri, tarihleri ve fiyatları RAKAM DEĞİL YAZIYLA yaz.
  Örnekler: "14:00" yerine "saat on dört", "09:30" yerine "saat dokuz buçuk",
  "350₺" yerine "üç yüz elli lira", "25 Eylül" yerine "yirmi beş Eylül",
  "0532 123 45 67" yerine "sıfır beş yüz otuz iki, yüz yirmi üç, kırk beş, altmış yedi".
- Kısaltma, sembol, emoji, madde işareti ve parantez kullanma. "vb." yerine "ve benzeri" de.
- Cümlelerin kısa olsun, bir seferde en fazla iki cümle söyle.

Görevlerin:
1. Arayan kişinin sorularını yukarıdaki bilgilere göre yanıtla. Bilmediğin bir şeyi uydurma;
   "Bu konuda sizi yetkili arkadaşımıza yönlendireyim" de.
2. Randevu isteyen kişiden adını, istediği hizmeti, günü ve saati al.
   Çalışma saatleri dışındaki bir saati önerme.
3. Bilgiler tamamlanınca randevu oluşturma aracını kullan ve sonucu arayana bildir.
4. Görüşmeyi nazikçe sonlandır."""


# ---------------------------------------------------------------------------
# Sağlık kontrolü (uygulama açılışta bunu çağırıp sunucuyu uyandırabilir)
# ---------------------------------------------------------------------------
@app.get("/")
def home():
    return {
        "durum": "aktif",
        "mesaj": "SesAI Sunucusu Çalışıyor!",
        "firebase": db is not None,
        "vapi": bool(VAPI_PRIVATE_KEY),
    }


# ---------------------------------------------------------------------------
# 1) Asistan oluştur / güncelle
# ---------------------------------------------------------------------------
class AssistantRequest(BaseModel):
    businessName: str
    voice: str  # "yunus" | "nergis" | "yagmur"
    sector: str = ""
    services: str = ""
    workingHours: str = ""
    extraInfo: str = ""
    greeting: str = ""


@app.post("/api/assistant")
async def create_or_update_assistant(d: AssistantRequest, authorization: str | None = Header(None)):
    require_setup()
    uid = get_uid(authorization)

    voice_key = d.voice.strip().lower().replace("ğ", "g")
    if voice_key not in VOICES:
        raise HTTPException(400, "Ses seçimi geçersiz. Yunus, Nergis veya Yağmur olmalı.")
    if not d.businessName.strip():
        raise HTTPException(400, "İşletme adı boş olamaz.")
    voice = VOICES[voice_key]

    user_ref = db.collection("users").document(uid)
    user = (user_ref.get().to_dict() or {})
    existing_id = user.get("vapiAssistantId")

    # Şablon asistanın ayarlarını al (model, dil, araçlar vb. aynen kalsın)
    template = await vapi("GET", f"/assistant/{TEMPLATE_ASSISTANT_ID}")
    body = {k: template[k] for k in TEMPLATE_KEYS if k in template}

    # Ses: şablonun ses ayarlarını koru, sadece sesi değiştir
    voice_cfg = dict(body.get("voice") or {})
    voice_cfg.setdefault("provider", "11labs")
    voice_cfg["voiceId"] = voice["voiceId"]
    body["voice"] = voice_cfg

    # Model: şablonun modelini koru, sistem talimatını müşteriye göre yaz
    model_cfg = dict(body.get("model") or {"provider": "openai", "model": "gpt-4o"})
    model_cfg["messages"] = [{"role": "system", "content": build_system_prompt(d, voice["name"])}]
    body["model"] = model_cfg

    body["name"] = f"SesAI - {d.businessName}"[:40]
    body["firstMessage"] = d.greeting.strip() or (
        f"Merhaba, {d.businessName}, hoş geldiniz! Ben {voice['name']}. Size nasıl yardımcı olabilirim?"
    )
    body["metadata"] = {"uid": uid}

    if existing_id:
        result = await vapi("PATCH", f"/assistant/{existing_id}", body)
    else:
        result = await vapi("POST", "/assistant", body)

    user_ref.set(
        {
            "vapiAssistantId": result["id"],
            "assistantVoice": voice_key,
            "assistantStatus": "ready",
            "assistantUpdatedAt": now_iso(),
        },
        merge=True,
    )
    return {"success": True, "assistantId": result["id"], "created": not existing_id}


# ---------------------------------------------------------------------------
# 2) Müşterinin kendi numarasını (SIP) bağla
# ---------------------------------------------------------------------------
class PhoneRequest(BaseModel):
    provider: str          # "netgsm" | "verimor" | "diger"
    phoneNumber: str       # örn. 08501234567 veya +908501234567
    sipServer: str         # operatörün SIP sunucu adresi / IP'si
    sipUsername: str
    sipPassword: str


def normalize_tr_number(n: str) -> str:
    digits = "".join(ch for ch in n if ch.isdigit())
    if digits.startswith("90"):
        return "+" + digits
    if digits.startswith("0"):
        return "+90" + digits[1:]
    return "+90" + digits


@app.post("/api/phone")
async def connect_phone(d: PhoneRequest, authorization: str | None = Header(None)):
    require_setup()
    uid = get_uid(authorization)

    user_ref = db.collection("users").document(uid)
    user = (user_ref.get().to_dict() or {})
    assistant_id = user.get("vapiAssistantId")
    if not assistant_id:
        raise HTTPException(400, "Önce asistanınızı oluşturun.")

    number = normalize_tr_number(d.phoneNumber)

    # Daha önce bağlanmış numara varsa önce onu kaldır
    for key, path in (("vapiPhoneNumberId", "/phone-number/"), ("vapiCredentialId", "/credential/")):
        old = user.get(key)
        if old:
            try:
                await vapi("DELETE", f"{path}{old}")
            except HTTPException:
                pass

    credential = await vapi(
        "POST",
        "/credential",
        {
            "provider": "byo-sip-trunk",
            "name": f"sesai-{uid[:8]}",
            "gateways": [{"ip": d.sipServer.strip()}],
            "outboundAuthenticationPlan": {
                "authUsername": d.sipUsername.strip(),
                "authPassword": d.sipPassword,
            },
        },
    )

    phone = await vapi(
        "POST",
        "/phone-number",
        {
            "provider": "byo-phone-number",
            "name": f"SesAI {number}",
            "number": number,
            "numberE164CheckEnabled": False,
            "credentialId": credential["id"],
            "assistantId": assistant_id,
        },
    )

    # SIP şifresi Firestore'a KAYDEDİLMEZ, sadece Vapi'de durur
    sip_uri = f"sip:{number}@{credential['id']}.sip.vapi.ai"
    user_ref.set(
        {
            "phoneNumber": number,
            "phoneProvider": d.provider,
            "vapiPhoneNumberId": phone["id"],
            "vapiCredentialId": credential["id"],
            "inboundSipUri": sip_uri,
            "phoneStatus": "connected",
            "phoneUpdatedAt": now_iso(),
        },
        merge=True,
    )
    return {"success": True, "phoneNumber": number, "inboundSipUri": sip_uri}


# ---------------------------------------------------------------------------
# 3) Vapi webhook: randevuları Firestore'a kaydet
# ---------------------------------------------------------------------------
def find_uid_by_assistant(assistant_id: str | None) -> str | None:
    if not assistant_id or db is None:
        return None
    docs = db.collection("users").where("vapiAssistantId", "==", assistant_id).limit(1).get()
    return docs[0].id if docs else None


def save_appointment(message: dict, params: dict) -> str:
    musteri_adi = params.get("customerName") or params.get("customer_name") or "Değerli Müşterimiz"
    tarih = params.get("appointmentTime") or params.get("date") or "Belirtilen saatte"
    hizmet = params.get("service") or "Randevu"
    print(f"Yeni Randevu İsteği: {musteri_adi} - {tarih} - {hizmet}")

    call = message.get("call") or {}
    assistant_id = call.get("assistantId") or (message.get("assistant") or {}).get("id")
    uid = find_uid_by_assistant(assistant_id)
    if uid and db is not None:
        db.collection("users").document(uid).collection("appointments").add(
            {
                "customerName": musteri_adi,
                "appointmentTime": tarih,
                "service": hizmet,
                "callerNumber": (call.get("customer") or {}).get("number", ""),
                "status": "new",
                "createdAt": now_iso(),
            }
        )
    return f"Sayın {musteri_adi}, {tarih} için {hizmet} randevunuz başarıyla oluşturuldu."


@app.post("/api/vapi/webhook")
async def vapi_webhook(request: Request):
    data = await request.json()
    message = data.get("message", {})
    mtype = message.get("type")

    # Eski format
    if mtype == "function-call":
        params = (message.get("functionCall") or {}).get("parameters") or {}
        text = save_appointment(message, params)
        return {"result": {"success": True, "message": text}}

    # Yeni format
    if mtype == "tool-calls":
        results = []
        for tc in message.get("toolCallList") or message.get("toolCalls") or []:
            args = (tc.get("function") or {}).get("arguments") or {}
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except ValueError:
                    args = {}
            results.append({"toolCallId": tc.get("id"), "result": save_appointment(message, args)})
        return {"results": results}

    return {"status": "ok"}


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
