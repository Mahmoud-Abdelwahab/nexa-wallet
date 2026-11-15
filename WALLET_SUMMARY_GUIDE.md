# Wallet Summary + Exchange Rates

تم تكييف تنفيذ [PR #11](https://github.com/Mahmoud-Abdelwahab/nexa-wallet/pull/11)
على الفرع المحلي الحالي. الـ endpoint داخل `app/api/routes/wallet.py`، قبل
`/{wallet_id}` عشان كلمة `summary` ما تتفسّرش كرقم محفظة.

```text
GET /wallets/summary?base_currency=AED
                  |
             JWT + sid check
                  |
          User wallets (PostgreSQL)
                  |
       محتاجين أسعار لعملات مختلفة؟
          |                    |
         لا                   نعم
          |                    |
       rate = 1        Redis FX cache (6 ساعات)
                               |
                    مفيش كاش صالح؟ Frankfurter v2
                               |
                  فحص العملات والقيم وتواريخ النشر
          |                    |
          +------> Decimal conversion
                          |
               تقريب السطور -> جمع الإجمالي
```

## تجربة الـ API

أعد تشغيل الـ API، أو أعد بناء Docker لو شغال بالحاويات. من Swagger استخدم
Access Token لجلسة نشطة، وجرّب:

```http
GET /wallets/summary?base_currency=AED
Authorization: Bearer <access_token>
```

العملات المتاحة هي `AED` و`USD` و`EUR`. لو مفيش query parameter نستخدم AED.
عملة خارج القائمة ترجع 422؛ محفظة SAR ليست جزءًا من هذا التغيير.

مثال بأرقام اختبار توضيحية، وليست أسعار صرف حقيقية:

```json
{
  "base_currency": "AED",
  "estimated_total": "500.00",
  "is_estimate": true,
  "rate_source": "Frankfurter (reference rates)",
  "rates_as_of": "2026-11-15",
  "wallets": [
    {
      "wallet_id": 1,
      "currency": "AED",
      "status": "ACTIVE",
      "balance": "100.0000",
      "rate_to_base": "1.000000000000",
      "estimated_in_base": "100.00",
      "rate_date": null
    },
    {
      "wallet_id": 2,
      "currency": "USD",
      "status": "ACTIVE",
      "balance": "100.0000",
      "rate_to_base": "4.000000000000",
      "estimated_in_base": "400.00",
      "rate_date": "2026-11-15"
    }
  ]
}
```

قيم Decimal بتظهر كنصوص في JSON للحفاظ على الدقة. `rate_to_base` هي قيمة وحدة
واحدة من عملة المحفظة بعملة العرض. أسعار Frankfurter مطلوبة بالاتجاه العكسي:
base=عملة العرض، quote=عملة المحفظة؛ لذلك نقسم الرصيد على السعر القادم من المصدر.

## قواعد الحساب والفشل

- بنقرأ محافظ المستخدم الموثّق فقط، ومن غير ما العميل يختار `user_id`.
- المحافظ غير النشطة أو الموقوفة تدخل في إجمالي الأموال المحتفظ بها، مع إظهار
  حالتها. الإجمالي ليس رصيدًا متاحًا للتحويل أو تصريحًا بالصرف.
- كل سطر يُقرّب لمنزلتين بـ `ROUND_HALF_UP`، والإجمالي مجموع السطور المعروضة.
- المحافظ من نفس عملة العرض لا تحتاج HTTP أو FX cache؛ قيمتها التحويلية 1.
- الحساب الفارغ يرجع إجمالي `0.00` وقائمة فارغة.
- نرفض سعرًا مفقودًا، مكررًا، غير موجب، غير محدود، بتاريخ مستقبلي، أو أقدم من
  7 أيام. التاريخ الظاهر للإجمالي هو أقدم تاريخ ضمن الأسعار المستخدمة.
- لو مصدر الأسعار تعطل ومفيش كاش صالح، نرجّع 503 برسالة عامة، بدون إجمالي جزئي.
- Redis تخزن أسعارًا عامة فقط لمدة 21600 ثانية؛ كاش تالفة/قديمة تؤدي لإعادة الجلب.
  عطل FX cache يسمح بمحاولة المصدر، لكن عطل Redis الخاصة بالتحقق من `sid` يظل
  يمنع الطلب المحمي بـ 503 طبقًا لفلو Authentication.
- لا يوجد تعديل لأرصدة أو Ledger أو Transfers، ولا جدول جديد أو migration.

المصدر يستخدم أسعارًا مرجعية للعرض، وليست أسعار تداول لحظية أو أسعار تنفيذ.
تفاصيل الخدمة واتجاه الأسعار في [توثيق Frankfurter](https://frankfurter.dev/).

## خريطة الكود

| الملف | المسؤولية |
| --- | --- |
| `app/api/routes/wallet.py` | Authentication، قراءة المحافظ خارج Event Loop، ورد 503 |
| `app/integrations/frankfurter_client.py` | HTTP، التحقق من الرد، كاش Redis |
| `app/services/wallet_summary_service.py` | حساب Decimal خالص بدون كتابة DB |
| `app/schemas/wallet_summary.py` | شكل الإجمالي وتفاصيل المحافظ وتواريخ الأسعار |
| `tests/test_wallet_summary.py` | اختبارات الحساب والمصدر والكاش والـ HTTP |

## الاختبارات

```bash
DEBUG=false .venv/bin/python -m pytest tests/test_wallet_summary.py -q
DEBUG=false .venv/bin/python -m pytest tests -q
```

اختبارات الوحدة لا تحتاج خدمة أسعار حقيقية أو Redis أو PostgreSQL؛ مصدر HTTP
محاكى، وبيانات المحافظ معزولة في SQLite. تشمل الحالات الثلاث المطلوبة، فشل
المصدر، البيانات غير الصالحة، حدود الدقة، ترتيب الـ route، عزل المستخدمين،
ومقارنة الأرصدة وعدد سجلات Ledger/Transaction قبل وبعد الطلب.
