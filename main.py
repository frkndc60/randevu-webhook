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
from google.cloud.firestore_v1.base_query import FieldFilter
from pydantic import BaseModel

# ---------------------------------------------------------------------------
# Ayarlar
# ---------------------------------------------------------------------------
SERVER_VERSION = "2026-09-26.2"
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
WEBHOOK_URL = "https://randevu-webhook.onrender.com/api/vapi/webhook"

# Randevu aracı: sunucu kendisi tanımlar (Vapi'deki eski araç kullanılmaz)
BOOK_TOOL = {
    "type": "function",
    "async": False,
    "function": {
        "name": "randevu_olustur",
        "description": "Müşteri için randevu oluşturur. Müşterinin adını, randevu gününü, saatini ve "
                       "istediği hizmeti öğrendikten SONRA çağır.",
        "parameters": {
            "type": "object",
            "properties": {
                "customerName": {"type": "string", "description": "Müşterinin adı ve soyadı"},
                "date": {"type": "string", "description": "Randevu günü, müşterinin söylediği gibi (ör. yarın, 27 Eylül, cuma)"},
                "time": {"type": "string", "description": "Randevu saati (ör. 15:00, öğleden sonra üç)"},
                "service": {"type": "string", "description": "İstenen hizmet (ör. saç kesimi)"},
                "phone": {"type": "string", "description": "Müşterinin telefon numarası, verdiyse"},
            },
            "required": ["customerName", "date", "time"],
        },
    },
    "server": {"url": WEBHOOK_URL},
    "messages": [
        {"type": "request-start", "content": "Hemen kaydınızı oluşturuyorum."},
        {"type": "request-failed", "content": "Kusura bakmayın, sistemde küçük bir sorun oldu, bir daha deneyeyim."},
    ],
}

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


def speakable(text: str) -> str:
    """Sesli okunacak metinden sembolleri temizler (& -> ve vb.)."""
    repl = {"&": " ve ", "+": " artı ", "%": " yüzde ", "@": " at ", "/": " ", "#": " ", "_": " ", "*": " "}
    for k, v in repl.items():
        text = text.replace(k, v)
    return " ".join(text.split())


def build_system_prompt(d: "AssistantRequest", voice_name: str) -> str:
    return f"""Sen "{speakable(d.businessName)}" işletmesinin telefon asistanısın. Adın {voice_name}.
Her zaman Türkçe, kibar, kısa ve doğal cümlelerle konuş.

İşletme bilgileri:
- İşletme adı: {speakable(d.businessName)}
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

Doğal konuşma kuralları (robot gibi değil, gerçek bir insan gibi konuş):
- Günlük, samimi ama saygılı Türkçe kullan. Resmi ve kitabi cümlelerden kaçın.
  "Size nasıl yardımcı olabilirim?" gibi kalıpları sürekli tekrarlama.
- Arada doğal bağlaçlar kullan: "Tabii", "Hemen bakıyorum", "Anladım",
  "Şöyle yapalım", "Harika". Ama her cümleye ekleme, abartma.
- Karşındakinin söylediğini kısaca onaylayarak cevaba başla:
  "Yarın öğleden sonra, anladım." gibi.
- Madde madde sayma, liste okuma. Seçenekleri sohbet eder gibi söyle:
  "Yarın saat üçte ya da dörtte boşluğumuz var, hangisi size uyar?"
- Karşındakinin adını öğrenince ara sıra adıyla hitap et, her cümlede değil.
- Müşteriye ASLA emir kipiyle konuşma ("bekleyin", "söyleyin", "tekrarlayın" gibi).
  Her zaman kibar rica ya da kendi eylemini anlatan cümle kur:
  "Bir saniye bekleyin" yerine "Hemen bakıyorum" veya "Bir saniye rica edeceğim",
  "Adınızı söyleyin" yerine "Adınızı alabilir miyim?",
  "Tekrar edin" yerine "Bir daha söyler misiniz?"
- Randevu kaydederken sessiz kalma, "Hemen kaydınızı oluşturuyorum" de.
- Emin olmadığın bir şeyi anlamadıysan doğal şekilde tekrar sor:
  "Pardon, tam duyamadım, hangi gün demiştiniz?"

Görevlerin:
1. Arayan kişinin sorularını yukarıdaki bilgilere göre yanıtla. Bilmediğin bir şeyi uydurma;
   "Bu konuda sizi yetkili arkadaşımıza yönlendireyim" de.
2. Randevu isteyen kişiden adını, istediği hizmeti, günü ve saati al.
   Çalışma saatleri dışındaki bir saati önerme.
3. Adı, günü, saati ve hizmeti öğrenince "randevu_olustur" aracını çağır.
   Müşterinin söylediği günü ve saati araca AYNEN ilet, bu bilgileri asla boş bırakma.
   Aracın cevabına göre müşteriye sonucu bildir.
4. Görüşmeyi nazikçe sonlandır."""


# ---------------------------------------------------------------------------
# Sağlık kontrolü (uygulama açılışta bunu çağırıp sunucuyu uyandırabilir)
# ---------------------------------------------------------------------------
@app.get("/")
def home():
    return {
        "durum": "aktif",
        "mesaj": "SesAI Sunucusu Çalışıyor!",
        "surum": SERVER_VERSION,
        "firebase": db is not None,
        "vapi": bool(VAPI_PRIVATE_KEY),
    }


# ---------------------------------------------------------------------------
# 1) Asistan oluştur / güncelle
# ---------------------------------------------------------------------------
class AssistantRequest(BaseModel):
    businessName: str
    voice: str  # "yunus" | "nergis" | "yagmur"
    assistantName: str = ""  # müşterinin verdiği asistan adı (boşsa sesin adı kullanılır)
    sector: str = ""
    services: str = ""
    workingHours: str = ""
    extraInfo: str = ""
    greeting: str = ""


@app.post("/api/assistant")
async def create_or_update_assistant(d: AssistantRequest, authorization: str | None = Header(None)):
    require_setup()
    uid = get_uid(authorization)
    return await apply_assistant(uid, d)


VOICE_ID_TO_KEY = {v["voiceId"]: k for k, v in VOICES.items()}


def _first(*vals) -> str:
    for v in vals:
        if isinstance(v, str) and v.strip():
            return v.strip()
    return ""


@app.post("/api/assistant/sync")
async def sync_assistant(authorization: str | None = Header(None)):
    """Uygulama sadece bunu çağırır; sunucu tüm bilgileri Firestore'dan kendisi okur."""
    require_setup()
    uid = get_uid(authorization)
    user = (db.collection("users").document(uid).get().to_dict() or {})
    cfg = user.get("assistantConfig") or {}
    setup = user.get("setup") or user.get("setupData") or {}

    voice_key = VOICE_ID_TO_KEY.get(cfg.get("elevenLabsVoiceId", ""), "")
    if not voice_key:
        name = _first(cfg.get("voiceName"), user.get("voice"), user.get("assistantVoice")).lower().replace("ğ", "g")
        voice_key = next((k for k in VOICES if name.startswith(k)), "yunus")

    d = AssistantRequest(
        businessName=_first(cfg.get("businessName"), user.get("businessName"), setup.get("businessName"), "İşletmemiz"),
        voice=voice_key,
        sector=_first(user.get("sector"), setup.get("sector"), cfg.get("sector")),
        services=_first(user.get("services"), setup.get("services"), cfg.get("services")),
        workingHours=_first(user.get("workingHours"), setup.get("workingHours"), cfg.get("workingHours")),
        extraInfo=_first(user.get("extraInfo"), setup.get("extraInfo"), cfg.get("extraInfo")),
        assistantName=_first(cfg.get("name")),
        greeting=_first(cfg.get("greetingMessage")),
    )
    result = await apply_assistant(uid, d)
    result.update({"voice": voice_key, "businessName": d.businessName, "assistantName": d.assistantName})
    return result


async def apply_assistant(uid: str, d: AssistantRequest) -> dict:
    voice_key = d.voice.strip().lower().replace("ğ", "g")
    if voice_key not in VOICES:
        raise HTTPException(400, "Ses seçimi geçersiz. Yunus, Nergis veya Yağmur olmalı.")
    if not d.businessName.strip():
        raise HTTPException(400, "İşletme adı boş olamaz.")
    voice = VOICES[voice_key]
    # Asistanın adı: uygulamada yazılan isim; boşsa seçilen sesin adı
    typed = d.assistantName.strip()[:30]
    assistant_name = (typed[:1].upper() + typed[1:]) if typed else voice["name"]

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
    # Dengeli tonlama: kelime sonlarını uzatmasın, net ve akıcı konuşsun
    voice_cfg["model"] = "eleven_turbo_v2_5"  # Turkcede daha akici, kelime sonlarini uzatmiyor
    voice_cfg["stability"] = 0.65       # yüksek = daha kararlı, uzatma/titreme az
    voice_cfg["similarityBoost"] = 0.8
    voice_cfg["style"] = 0.0            # 0 = abartılı tonlama yok
    voice_cfg["useSpeakerBoost"] = True
    body["voice"] = voice_cfg

    # Model: şablonun modelini koru, sistem talimatını müşteriye göre yaz
    model_cfg = dict(body.get("model") or {"provider": "openai", "model": "gpt-4o"})
    model_cfg["messages"] = [{"role": "system", "content": build_system_prompt(d, assistant_name)}]
    model_cfg.pop("toolIds", None)          # şablondaki eski randevu aracını kullanma
    model_cfg["tools"] = [BOOK_TOOL]
    body["model"] = model_cfg

    body["name"] = f"SesAI - {d.businessName}"[:40]
    # Karşılama cümlesi: uygulamada yazılan cümle; boşsa otomatik
    body["firstMessage"] = speakable(d.greeting.strip()) if d.greeting.strip() else (
        f"Merhaba, {speakable(d.businessName)}, hoş geldiniz! Ben {assistant_name}. Size nasıl yardımcı olabilirim?"
    )
    # Asistan her zaman önce konuşsun ve SADECE bizim karşılama cümlemizi söylesin
    body["firstMessageMode"] = "assistant-speaks-first"
    body["metadata"] = {"uid": uid}

    if existing_id:
        result = await vapi("PATCH", f"/assistant/{existing_id}", body)
    else:
        result = await vapi("POST", "/assistant", body)

    user_ref.set(
        {
            "vapiAssistantId": result["id"],
            "assistantVoice": voice_key,
            "voice": voice_key,
            "businessName": d.businessName,
            "assistantName": assistant_name,
            "assistantStatus": "ready",
            "assistantUpdatedAt": now_iso(),
        },
        merge=True,
    )
    return {"success": True, "assistantId": result["id"], "created": not existing_id,
            "firstMessage": body["firstMessage"]}


# ---------------------------------------------------------------------------
# Teşhis: uygulamadaki "Sistem Testi" bu ucu çağırır
# ---------------------------------------------------------------------------
@app.get("/api/diagnose")
async def diagnose(authorization: str | None = Header(None)):
    report: dict = {"sunucuSurumu": SERVER_VERSION, "firebase": db is not None, "vapiAnahtari": bool(VAPI_PRIVATE_KEY)}
    require_setup()
    uid = get_uid(authorization)
    report["girisDogrulandi"] = True

    user = (db.collection("users").document(uid).get().to_dict() or {})
    report["firestore"] = {
        "vapiAssistantId": user.get("vapiAssistantId", ""),
        "assistantVoice": user.get("assistantVoice", ""),
        "businessName": user.get("businessName", ""),
        "setupComplete": user.get("setupComplete", False),
        "assistantUpdatedAt": user.get("assistantUpdatedAt", ""),
        "phoneStatus": user.get("phoneStatus", ""),
    }

    aid = user.get("vapiAssistantId")
    if not aid:
        report["vapiAsistani"] = "YOK - önce kurulumu tamamlayın"
        return report
    try:
        a = await vapi("GET", f"/assistant/{aid}")
    except HTTPException as e:
        report["vapiAsistani"] = f"HATA: {e.detail}"
        return report

    voice = a.get("voice") or {}
    voice_name = next((v["name"] for v in VOICES.values() if v["voiceId"] == voice.get("voiceId")), "BILINMEYEN SES")
    msgs = (a.get("model") or {}).get("messages") or []
    prompt = next((m.get("content", "") for m in msgs if m.get("role") == "system"), "")
    report["vapiAsistani"] = {
        "ad": a.get("name"),
        "karsilamaCumlesi": a.get("firstMessage"),
        "karsilamaModu": a.get("firstMessageMode"),
        "ses": f"{voice_name} ({voice.get('provider')} / {voice.get('model')})",
        "sesHizi": voice.get("speed"),
        "model": f"{(a.get('model') or {}).get('provider')} / {(a.get('model') or {}).get('model')}",
        "transkripsiyon": f"{(a.get('transcriber') or {}).get('provider')} / {(a.get('transcriber') or {}).get('language')}",
        "talimatIlkSatir": prompt.splitlines()[0] if prompt else "",
        "sonGuncelleme": a.get("updatedAt"),
    }
    return report


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
    docs = db.collection("users").where(filter=FieldFilter("vapiAssistantId", "==", assistant_id)).limit(1).get()
    return docs[0].id if docs else None


def _pick(params: dict, *keys) -> str:
    for k in keys:
        v = params.get(k)
        if isinstance(v, (str, int, float)) and str(v).strip():
            return str(v).strip()
    return ""


def save_appointment(message: dict, params: dict) -> str:
    print(f"Randevu aracı parametreleri: {json.dumps(params, ensure_ascii=False)}")
    musteri_adi = _pick(params, "customerName", "customer_name", "name", "fullName", "ad", "adSoyad", "musteri")
    tarih = _pick(params, "appointmentTime", "appointment_time", "datetime", "dateTime", "date_time",
                  "appointmentDate", "appointment_date", "date", "tarih")
    saat = _pick(params, "time", "saat", "hour")
    if saat and saat not in tarih:
        tarih = f"{tarih} {saat}".strip()
    hizmet = _pick(params, "service", "serviceName", "service_name", "hizmet", "islem", "reason", "notes")
    telefon = _pick(params, "phone", "phoneNumber", "phone_number", "telefon")

    eksik = [ad for ad, deger in (("müşterinin adı", musteri_adi), ("randevu günü ve saati", tarih)) if not deger]
    if eksik:
        print(f"Randevu eksik bilgi: {eksik}")
        return ("Randevu henüz KAYDEDİLMEDİ çünkü şu bilgi eksik: " + ", ".join(eksik) +
                ". Müşteriden bu bilgiyi kibarca iste, sonra randevu aracını tekrar çağır.")

    call = message.get("call") or {}
    assistant_id = call.get("assistantId") or (message.get("assistant") or {}).get("id")
    uid = find_uid_by_assistant(assistant_id)
    print(f"Yeni Randevu: {musteri_adi} - {tarih} - {hizmet or '-'} (uid: {uid})")
    if uid and db is not None:
        db.collection("users").document(uid).collection("appointments").add(
            {
                "customerName": musteri_adi,
                "appointmentTime": tarih,
                "service": hizmet,
                "customerPhone": telefon,
                "callerNumber": (call.get("customer") or {}).get("number", ""),
                "status": "new",
                "createdAt": now_iso(),
            }
        )
    return (f"Randevu başarıyla kaydedildi. Müşteri: {musteri_adi}. Zaman: {tarih}. "
            f"Hizmet: {hizmet or 'belirtilmedi'}. Şimdi müşteriye randevusunun oluşturulduğunu "
            f"kısa ve sıcak bir cümleyle söyle, başka bir isteği olup olmadığını sor.")


@app.post("/api/vapi/webhook")
async def vapi_webhook(request: Request):
    data = await request.json()
    message = data.get("message", {})
    mtype = message.get("type")
    if mtype in ("function-call", "tool-calls"):
        print(f"Vapi webhook: {mtype}")

    # Eski format
    if mtype == "function-call":
        params = (message.get("functionCall") or {}).get("parameters") or {}
        if isinstance(params, str):
            try:
                params = json.loads(params)
            except ValueError:
                params = {}
        return {"result": save_appointment(message, params)}

    # Yeni format
    if mtype == "tool-calls":
        results = []
        for tc in message.get("toolCallList") or message.get("toolCalls") or []:
            fn = tc.get("function") or {}
            args = fn.get("arguments") or tc.get("arguments") or {}
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
