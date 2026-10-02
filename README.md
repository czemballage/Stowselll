# StowSell — Railway Ready

نسخة جاهزة للنشر على Railway لبوت متجر Telegram المرتبط بـ ProdSeller Public API v1.

## التشغيل على Railway من الهاتف

1. ارفع هذا المشروع إلى GitHub كـ repository جديد (يمكن فعل ذلك من متصفح الهاتف).
2. افتح Railway وسجّل الدخول بحساب GitHub.
3. اختر **New Project → Deploy from GitHub Repo** ثم اختر مستودع StowSell.
4. في Railway افتح **Variables** وأضف:
   - `BOT_TOKEN` = توكن Telegram
   - `PRODSELLER_API_KEY` = مفتاح ProdSeller
   - `ADMIN_ID` = `7631570288`
   - `DEFAULT_MARKUP_PERCENT` = `20`
   - `BINANCE_PAYMENT_TEXT` = تعليمات الدفع
   - `BINANCE_PAY_TO` = عنوان/معرف الدفع
   - `SUPPORT_USERNAME` = حساب الدعم (اختياري)
5. اضغط Deploy. سيبدأ Railway تشغيل `python bot.py` تلقائياً.

لا ترفع ملف `.env` إلى GitHub. ضع الأسرار في Railway Variables فقط.

## مهم بخصوص قاعدة البيانات

البوت يستخدم SQLite. التشغيل العادي يعمل مباشرة، لكن إذا أردت الاحتفاظ بالبيانات حتى بعد إنشاء Deploy جديد، أضف Railway Volume واربطه بالمجلد `/data` ثم غيّر المتغير:

`DB_PATH=/data/stowsell.db`

إذا لم تضف Volume، سيعمل البوت لكن بيانات SQLite قد لا تبقى بعد إعادة نشر الخدمة.

## ماذا يحتوي البوت؟

- مزامنة المنتجات تلقائياً من ProdSeller.
- التحكم بالأسعار من لوحة الإدارة.
- هامش ربح افتراضي.
- إظهار/إخفاء المنتجات.
- التصنيفات والبحث.
- تعديل الوصف والصورة والتصنيف.
- واجهة متجر Telegram.
- دفع Binance يدوي بدون Binance API.
- إثبات دفع بالصور.
- تأكيد/رفض من الإدارة.
- تنفيذ شراء ProdSeller بعد التأكيد.
- Idempotency لمنع الطلبات المكررة.
- دعم منتجات تفعيل البريد.
- متابعة طلبات ProdSeller حتى التسليم.
- سجل الطلبات والعملاء.
- إحصائيات المبيعات والتكلفة والربح.
- مزامنة تلقائية كل 5 دقائق.

## التشغيل المحلي

```bash
pip install -r requirements.txt
python bot.py
```
