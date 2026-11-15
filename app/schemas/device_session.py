# الملف ده فيه شكل بيانات الجهاز الداخلة وشكل الجلسة اللي بنرجّعه للموبايل.
# DeviceMetadata -> Login/Register -> RefreshSession في Redis
# DeviceSessionResponse -> شاشة Devices & Sessions فقط، من غير أي توكنات.
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class DeviceMetadata(BaseModel):
    # اسم الجهاز والمنصة جايين من العميل للعرض فقط؛ مش إثبات لهوية الجهاز.
    # Field بيمنع الاسم الفاضي أو الطويل، وLiteral بيمنع منصات خارج القيم المحددة.
    device_name: str = Field(default="Unknown device", min_length=1, max_length=100)
    platform: Literal["iOS", "Android", "Web", "Unknown"] = "Unknown"


class DeviceSessionResponse(BaseModel):
    # دي البيانات الآمنة للعرض؛ تعمّدنا ما نرجّعش refresh token أو الهاش أو user_id.
    session_id: str
    # السيرفر يقارنها بـ sid الموثّقة في JWT، مش باسم الجهاز اللي العميل بعته.
    # الموبايل يعلّم الجهاز الحالي ويخفي زر الحذف لما القيمة تكون True.
    is_current: bool
    device_name: str
    platform: str
    # None يسمح بقراءة بيانات قديمة ماكانش فيها تواريخ، لو باقي شروط النشاط متحققة.
    created_at: datetime | None
    last_seen_at: datetime | None
