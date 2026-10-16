'use strict';
const button=document.getElementById('email-preview');
if(button) button.addEventListener('click',async()=>{
 const output=document.getElementById('email-text');button.disabled=true;
 try{const response=await fetch('/api/email-preview',{credentials:'same-origin',cache:'no-store'});
 if(!response.ok)throw new Error('unavailable');const data=await response.json();
 output.textContent=(data.subject||'')+'\n\n'+(data.text||'');}
 catch(error){output.textContent='התצוגה אינה זמינה כרגע. לא נשלח מייל.';}
 finally{button.disabled=false;}
});
