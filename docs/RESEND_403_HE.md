# בדיקת 403 של Resend ללא שליחה

בקשת המערכת היא POST ל־https://api.resend.com/emails, עם Authorization Bearer, Content-Type application/json ומפתח Idempotency-Key. גוף הבקשה כולל from, רשימת to, subject ותוכן text/html. השולח יכול להיות onboarding@resend.dev או Name <onboarding@resend.dev>. החוזה נבדק מול התיעוד הרשמי ונוסף לו כיסוי רגרסיה. כותרות Accept ו־User-Agent מזהות את הלקוח במפורש; אין הוכחה שכותרת הלקוח גרמה לכשל הייצור.

התווית http_response_origin_unverified הישנה ניתנת לכל HTTPError. היא אינה סיבה מ־Resend, אינה אומרת שהדומיין לא מאומת ואינה מוכיחה שהמפתח שגוי. גוף ה־403 המקורי לא נשמר ולכן אי אפשר לשחזר ממנו את הסיבה בדיעבד. אין בסביבה גישה למסד או ללוגים של Render.

מקורות רשמיים:
- https://resend.com/docs/api-reference/emails/send-email
- https://resend.com/docs/api-reference/errors
- https://resend.com/docs/knowledge-base/403-error-resend-dev-domain

onboarding@resend.dev נתמך לשליחה לכתובת הרשומה בחשבון Resend. כאשר הודעת השגיאה היא "You can only send testing emails to your own email address", הנמען חייב להיות אותה כתובת בדיוק. אין צורך בדומיין חדש כדי לשלוח לכתובת הרשומה. שגיאת "domain is not verified" היא מגבלה אחרת של השולח; אין להסיק אותה מקוד 403 בלבד. הצלחת שליחה ישירה עם אותו מפתח אינה חושפת את השולח והנמען שהוקפאו ברשומת outbox של המערכת.

## בדיקה ללא שליחה לאחר פריסת master

ב־Render Shell בשירות הקיים, להחליף OUTBOX_ID במזהה הניסיון שנכשל:

```sh
python -m rentalsourcing.cloud --inspect-email OUTBOX_ID
```

הפקודה מציגה מצב רשומה, ניסיון קודם, התאמת המעטפת להגדרות, שימוש בשולח onboarding, מספר נמענים וחוזה בקשה ללא מפתח או כתובות. last_error מופיע רק לשגיאות שנלכדו בקוד המעודכן. היא אינה מבצעת קריאת RentCast או שליחת מייל.

לרשומה ישנה ללא ראיה יש לבדוק Resend → Logs את בקשת POST /emails שנכשלה: השדות name/message בתגובה, ו־from/to בבקשה, מול כתובות בדיקת השליחה שהצליחה. אין לשתף Authorization או API key. כתובות יכולות להיות מושחרות בעת שיתוף הודעת השגיאה.

## החלפת ניסיון שנדחה בוודאות — ללא שליחה

```sh
python -m rentalsourcing.cloud --replace-rejected-test OUTBOX_ID
```

הפקודה מצליחה רק למייל ניסיון שסומן failed, עם ראיית דחייה מובנית שנשמרה, ללא provider_id וללא אירועי נכסים. היא שומרת את הרשומה המקורית ויוצרת תחליף קבוע עם אותה מעטפת מאושרת. הרצה חוזרת מחזירה אותו תחליף. היא לא שולחת. רשומה ישנה ללא ראיה או ניסיון uncertain אינם מוחלפים אוטומטית. גם דחייה מאוחרת אינה מבטלת אי־ודאות מניסיון קודם.

שליחה נוספת דורשת אישור מפורש. לאחריו אפשר לשלוח את מזהה התחליף ולבדוק מסירה דרך --email-status או אירוע Delivered ב־Resend. מפתח בעל הרשאת Sending בלבד עשוי שלא לאפשר קריאת סטטוס; זו אינה הוכחה שהשליחה נכשלה, ואין להחליף מפתח על סמך כך.

התזמון נשאר 08:00 Asia/Jerusalem, המיקודים 15203/15204/15210 נשארים מוחרגים, והמייל היומי משתמש בנתונים שמורים בלבד. אין שינוי מכסה או מסלול RentCast.
