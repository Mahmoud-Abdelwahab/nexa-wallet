docker compose up -d  =====>  to start runing the containsers we have two now postgress and redis
uvicorn app.main:app --reload  ====> to run the server 



docker compose up -d      # شغّل
docker compose down       # قفل واحذف الكونتينرات (الـ volume بيفضل)
docker compose logs -f    # شوف اللوجز لايف

docker ps                    # الكونتينرات الشغالة
docker ps -a                 # كل الكونتينرات حتى الواقفة
docker images                # الصور المحمّلة عندك
docker logs -f my-redis      # اللوجز
docker stop my-redis         # وقّف
docker start my-redis        # شغّل تاني
docker rm my-redis           # امسح (لازم يكون واقف الأول)
docker volume ls             # الفوليومز
docker system prune          # نضّف الحاجات غير المستخدمة



<!-- ============================== Business Roles =================================== -->
<!-- User Ownership: A Wallet must belong to a specific User.

Wallet Currency: The currency is an intrinsic attribute of the Wallet itself.

Non-Negative Balance: The Wallet balance cannot be negative (balance >= 0).

Unique Currency per User: A User cannot have duplicate Wallets for the same currency (Enforced via a unique constraint on user_id and currency).

Immutable Direct Updates: The balance cannot be updated directly or arbitrarily via an API endpoint; all balance changes must occur strictly through formal Transaction/Ledger operations. -->

---------------
POST /wallets
GET  /wallets
GET  /wallets/{wallet_id}