# ניסיון הפריסה הבא ב־Render — 16 באוקטובר 2026

## מה נבדק ותוקן

render.yaml נמצא בשורש guypenso/gps14 ומצביע ל־master. הקובץ המקורי והמעודכן עברו
את סכמת JSON הרשמית של Render (Draft 2020-12) עם אפס שגיאות. אין בידינו פירוט
הודעת "a few issues" מחשבון המשתמש, ולכן לא זוהתה סיבתה ואין טענה שתוקנה שגיאה ידועה במסך.
ה־Validator של Render CLI משתמש ב־API וב־Workspace מאומתים; הרשאות אלה לא זמינות כאן.

הגדרות ההשקה הוקשחו: Python 3.12.10 גם בקובץ .python-version; PostgreSQL 17 שנבדק;
אחסון DB בגודל 5GB עם storageAutoscalingEnabled=false; previews.generation=off.
הוסרו בקשות הזנת ארבעת סודות הספקים מהקמת ה־Blueprint: השירותים כבויים ואינם צריכים
אותם כדי להיבנות ולעלות. אין ערכי API keys או סיסמאות קבועות בקוד או ב־Blueprint.

190 בדיקות עברו מול PostgreSQL/SQLite, ושלושה דילוגי SQLite צפויים. התקנה נקייה של
requirements.txt, pip check, בדיקת סקריפט shell ובניית האפליקציה עברו. פקודת start_web.sh
אותחלה בפועל מול PostgreSQL ריק ללא שום סוד ספק: migrations, healthz=200, דף ללא
הרשאה=401, ודף/API/readyz עם הרשאה=200. usage ו־outbox נותרו ריקים.

## צעדים מדויקים למשתמש

1. Render → New → Blueprint / New Blueprint Instance → לבחור guypenso/gps14.
2. לבחור master ואת render.yaml שבשורש; לבדוק שמוצגים Web אחד, Cron אחד ו־PostgreSQL אחד.
3. אין צורך להזין מפתחות ספקים כדי להקים את השירותים הכבויים. DATABASE_URL מקושר
   אוטומטית לשני השירותים; Render יוצר DASHBOARD_PASSWORD. שם המשתמש owner.
4. "Payment Information Required" היא דרישת אמצעי תשלום למשאבים בתשלום, ולא תקלה
   שניתן לפתור בקוד. הוסף כרטיס בחשבון Render רק אם אתה מסכים לתשלום. ייתכן אימות
   כרטיס נפרד לפי מדיניות Render. ההוצאה על השירותים מתחילה עם אישור יצירתם/הקמתם
   ב־Deploy Blueprint / Apply, ולא ב־GitHub commit או בעצם בחירת המאגר.
5. לפני אישור היצירה, לבדוק מחיר בפועל במסך. לא נוצרו משאבים על ידינו. לאחר אישור,
   להמתין שה־DB יהיה Available ושה־Web יהיה Live; ה־Cron אמור להשלים Build ללא שגיאה.
6. לפתוח את כתובת HTTPS של Web. /healthz אמור להחזיר {"status":"ok"}. בדף הראשי
   להתחבר עם owner והסיסמה הפרטית מ־Web → Environment → DASHBOARD_PASSWORD.
   /readyz לאחר התחברות צריך להחזיר ready ו־boundary_verified=true.
7. רק לאחר עלייה תקינה, ב־rentalsourcing-daily → Environment להוסיף בפרטיות:
   RENTCAST_API_KEY, RESEND_API_KEY, EMAIL_FROM (שולח מאומת), EMAIL_TO (כתובתך).
   לא להוסיף סודות אלה ל־Web או למאגר. אין להדביק אותם בצ׳אט.
8. להשאיר ENABLE_RENTCAST=false ו־ENABLE_EMAIL=false ו־APPROVED_API_REQUESTS=0.
   ארבע הקריאות הקודמות נוצלו. הפעלת סריקות דורשת אישור מכסה מתמשכת חדשה;
   בדיקת מייל ושליחה אמיתית דורשות אישור נפרד. דשבורד ריק לפני העברת נתונים צפוי.

אם הודעת "a few issues" עדיין מופיעה, פתח את פירוט השגיאות לפני Deploy. הסכמה אינה
בודקת מגבלות Workspace, הרשאות GitHub, זמינות מסלולים בחשבון או אמצעי תשלום.
אין כאן הוכחה שהפריסה בענן הצליחה; רק ניסיון הפריסה המאומת בחשבון יקבע זאת.

## תזמון, נתונים ועלות

Cron יחיד: 0 12,13 * * * ב־UTC. --scheduled מאפשר רק 08:00 America/New_York כולל DST;
החלון האחר יוצא בלי קריאות. ה־Web אינו סורק; נעילת PostgreSQL ומכסה מתמשכת מגינות
מכפילות. אין צורך בדיסק Web או Cron: הנתונים, התור וה־outbox נשמרים ב־PostgreSQL.

אומדן בסיס Render: Web Starter $7 + Postgres 0.1c-256mb $6 + אחסון 5GB עד $1.50 +
Cron לפחות $1 = כ־$15.50 לחודש ב־Workspace חינמי. אין Redis, Worker נוסף או Preview.
Cron לפי זמן ריצה עשוי לעבור את המינימום; תעבורה, דקות Build, מסים ושינוי מחיר נוספים
אינם כלולים. אין מנגנון שמבטיח תקרת חשבון של $15.50. מחיר ההזמנה הוא הקובע.

RentCast נפרד: עד 50 קריאות חודשיות כלולות במסלול Developer אם אין שימוש אחר;
נפח מלא ואימות כל המועמדים לא מובטחים במכסה זו. אין אישור לצריכה חדשה.
Resend נפרד: מייל יומי יחיד צפוי להתאים למסלול החינמי, בכפוף למגבלות החשבון ושולח מאומת.

להעברת המדגם בלי קריאת ספק: python -m rentalsourcing.transfer עם SOURCE_DATABASE_URL
ו־DATABASE_URL פרטיים. הכלי דורש יעד ריק ושומר את מכסת ארבע הקריאות והיסטוריית המיילים.
אין להעלות גיבוי נתונים או סודות למאגר הציבורי.

מקורות: https://render.com/schema/render.yaml.json ; https://render.com/docs/blueprint-spec ;
https://render.com/docs/python-version ; https://render.com/docs/cronjobs ; https://render.com/pricing ;
https://render.com/articles/production-rails-hosting-guide ; https://render.com/docs/deploy-openclaw .
