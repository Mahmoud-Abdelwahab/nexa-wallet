# Wallet statement CSV export

تنفيذ [PR #12](https://github.com/Mahmoud-Abdelwahab/nexa-wallet/pull/12) متكيّف مع
الفرع المحلي الحالي. الـ endpoint موجودة داخل `app/api/routes/wallet.py`، مع
باقي Wallet routes، من غير جدول جديد أو dependency أو مصدر خارجي.

```text
GET /wallets/{wallet_id}/statement
                 |
           JWT + sid active?
                 |
       validate dates + CSV format
                 |
         wallet belongs to user?
                 |
     one SELECT: Ledger JOIN Transaction
     WHERE wallet/time, ORDER BY time/id
             LIMIT 5001
                 |
       > 5000? -> 413, no partial CSV
                 |
       escape text -> CSV + UTF-8 BOM
                 |
       attachment + no-store + nosniff
```

## التجربة من Swagger

1. أعد تشغيل الـ API، أو أعد بناء صورة Docker لو شغال بالحاويات.
2. افتح `/docs` واعمل Authorize باستخدام Access Token لجلسة نشطة.
3. اختَر `GET /wallets/{wallet_id}/statement`، واستخدم محفظة ملك الحساب.
4. اختَر `format=csv` وحدد التواريخ أو سيبهم للمدة الافتراضية.
5. Execute ثم حمّل الملف وافتحه في Excel أو قارئ CSV.

```http
GET /wallets/1/statement?format=csv&start_date=2026-11-01&end_date=2026-11-15
Authorization: Bearer <access_token>
```

استخدم رقم محفظتك وفترة فيها حركات فعلًا. عدم وجود حركات يرجع ملفًا فيه أسماء
الأعمدة فقط؛ لا ينشئ معاملات تجريبية في حسابك.

| Parameter | السلوك |
| --- | --- |
| `wallet_id` | محفظة مملوكة للمستخدم الموثّق |
| `format` | `csv` فقط؛ الافتراضي `csv` |
| `end_date` | آخر يوم، شامل؛ الافتراضي تاريخ اليوم على السيرفر |
| `start_date` | أول يوم، شامل؛ الافتراضي `end_date - 29 days` |

الافتراضي 30 يوم تقويمي شامل اليومين، وليس آخر 720 ساعة من لحظة الطلب. الفترة
القصوى 366 يوم تقويمي شامل الطرفين. التواريخ المستقبلية أو البداية بعد النهاية
مرفوضة. الفلتر يستخدم بداية أول يوم حتى بداية اليوم التالي لآخر يوم بشكل حصري،
عشان يشمل حركة الساعة `23:59:59.999999` ويستبعد منتصف ليل اليوم التالي.

حاليًا Ledger تستخدم timestamps بدون منطقة زمنية (`datetime.now` / naïve).
التصدير يحافظ على نفس دلالة الوقت المخزن، ولا يحوّله تلقائيًا لتوقيت الموبايل أو
UTC. التوحيد الزمني لكل بيانات المشروع يحتاج قرارًا منفصلًا.

## محتوى الملف

| العمود | المعنى |
| --- | --- |
| `created_at` | توقيت حركة Ledger كما هو مخزن |
| `ledger_entry_id` | رقم حركة Ledger |
| `transaction_id` | رقم المعاملة المرتبطة |
| `transaction_reference` | المرجع النصي للمعاملة |
| `entry_type` | مثل TOP_UP أو TRANSFER أو FEE |
| `direction` | CREDIT أو DEBIT |
| `amount` | مبلغ موجب بأربع منازل عشرية |
| `currency` | عملة الحركة الأصلية |
| `signed_amount` | موجب للـ CREDIT وسالب للـ DEBIT |

الترتيب من الأقدم للأحدث، ثم `ledger_entry_id` عند تساوي التوقيت. نصدّر حركات
Ledger الموجودة، وليس طلبات TopUp المعلّقة التي لم تنتج حركة Ledger. لا يوجد FX
أو جمع أرصدة مختلفة أو تعديل رصيد. المحفظة الموقوفة تقدر تصدّر سجلها طالما ملكك.
ده كشف حركات CSV؛ لا يحتوي رصيد افتتاحي/ختامي متصالح عليه أو اعتماد بنكي رسمي.

## الحدود وحماية البيانات

- الملكية تتفحص قبل قراءة حركات Ledger. محفظة غير موجودة أو مش ملك المستخدم
  ترجع نفس `404 Wallet not found`.
- حد SQL هو 5001 لاكتشاف تجاوز 5000؛ لو العدد أكبر نرفض التحميل كله بدل القطع.
- `csv.writer` يهرب الفواصل وعلامات الاقتباس، وكل الخلايا مقتبسة.
- المرجع النصي تُزال منه CR/LF/TAB ويُضاف له apostrophe لو شكله formula، مع
  دعم الرموز كاملة العرض. المبالغ الرقمية مصدرها Decimal من Ledger.
- الهروب مخصص للملف المُصدّر؛ إعادة حفظه أو معالجته في برامج أخرى ممكن تغيّر
  طريقة تفسير الخلايا. مرجع هذه المعالجة: [OWASP CSV Injection](https://owasp.org/www-community/attacks/CSV_Injection).
- UTF-8 BOM يحافظ على قراءة النص العربي في برامج الجداول المتوافقة.
- `Content-Disposition: attachment` يحدد اسم الملف، و`Cache-Control: no-store`
  تمنع التخزين المؤقت، و`X-Content-Type-Options: nosniff` تحدد تفسير نوع المحتوى.
- `sid` مطلوبة مثل باقي APIs المحمية؛ جهاز اتلغى لا يقدر يحمّل كشفًا بتوكنه القديمة.

| الحالة | HTTP |
| --- | --- |
| CSV ناجحة، حتى لو بدون حركات | 200 |
| فترة معكوسة/مستقبلية/أكبر من 366 يوم | 400 |
| بدون توكن أو جلسة ملغية | 401 |
| المحفظة غير موجودة أو غير مملوكة | 404 |
| أكثر من 5000 حركة داخل الفترة | 413 |
| صيغة غير CSV أو تاريخ غير صالح | 422 |

## خريطة الكود والاختبارات

- `app/api/routes/wallet.py`: بيانات الطلب، أكواد HTTP، توثيق Swagger، ومرفق الملف.
- `app/services/wallet_statement_service.py`: المدة والملكية والتنسيق وهروب النصوص.
- `app/repositories/wallet_statement_repository.py`: الاستعلام المحدود والمرتب.
- `tests/test_wallet_statement_export.py`: اختبارات مع SQLite معزولة، بدون بيانات
  المستخدم الحقيقية أو أي تعديل في قاعدة بياناته.

```bash
DEBUG=false .venv/bin/python -m pytest tests/test_wallet_statement_export.py -q
DEBUG=false .venv/bin/python -m pytest tests -q
```

الاختبارات تشمل تحميل 5000 صف فعلًا ورفض 5001، حدود أول وآخر يوم، الترتيب،
الملكية، الملف الفارغ، UTF-8 والنص العربي، هروب formulas، العمل خارج Event Loop،
وعدم تغيير الأرصدة أو عدد حركات Ledger/Transaction.
