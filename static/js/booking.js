(() => {
  'use strict';
  const config = JSON.parse(document.getElementById('booking-data').textContent);
  const $ = id => document.getElementById(id);
  let selectedPatient = null, activeHold = null, chosenSlot = null, busy = false, loadId = 0, lastSuccess = 0;
  const keys = new Map();
  const csrf = document.querySelector('[name=csrfmiddlewaretoken]').value;
  const node = (tag, text, cls) => { const el = document.createElement(tag); if (text) el.textContent = text; if (cls) el.className = cls; return el; };
  const error = message => { $('booking-error').textContent = message; $('booking-error').hidden = false; $('booking-error').focus(); };
  const clearError = () => { $('booking-error').hidden = true; };
  const localTime = value => new Date(value).toLocaleTimeString([], {hour:'2-digit',minute:'2-digit',timeZone:config.facilities.find(f=>String(f.id)===$('facility').value)?.timezone || 'Asia/Colombo'});
  async function api(url, method='GET', body) {
    const identity = method + url + JSON.stringify(body || {});
    if (!keys.has(identity)) keys.set(identity, crypto.randomUUID());
    const response = await fetch(url, {method, credentials:'same-origin', headers:{'Content-Type':'application/json','X-CSRFToken':csrf,'Idempotency-Key':keys.get(identity)}, ...(body ? {body:JSON.stringify(body)} : {}), signal:AbortSignal.timeout(12000)});
    let data; try {data=await response.json();} catch {throw new Error('The server could not complete the request. Please retry.');}
    if (!response.ok) { const e = new Error(data.message || JSON.stringify(data)); e.status = response.status; e.fields = data.field_errors; throw e; }
    return data;
  }
  function options(id, values, placeholder) {
    const select=$(id), old=select.value; select.replaceChildren();
    if (placeholder) select.append(new Option(placeholder,''));
    values.forEach(v=>select.append(new Option(v.name,v.id)));
    if ([...select.options].some(o=>o.value===old)) select.value=old;
  }
  options('facility',config.facilities);
  $('visit-date').value=config.date; $('visit-date').min=config.today;
  function filterOptions() {
    const scope = values=>values.filter(v=>String(v.facility_id)===$('facility').value);
    options('specialty',scope(config.specialties),'All specialties');
    options('doctor',scope(config.doctors).filter(d=>!$('specialty').value || String(d.specialty_id)===$('specialty').value),'All doctors');
    options('service',scope(config.services),'All services');
  }
  filterOptions();
  $('facility').addEventListener('change',()=>{selectedPatient=null;if($('selected-patient'))$('selected-patient').textContent='Choose a patient for this clinic.';filterOptions();load();});
  $('specialty').addEventListener('change',()=>{filterOptions();load();});
  ['doctor','service','visit-date'].forEach(id=>$(id).addEventListener('change',load));
  $('filters').addEventListener('submit',e=>{e.preventDefault();load();});
  async function load() {
    const requestId=++loadId;
    if (!$('facility').value) {$('freshness').textContent='No clinic is assigned to this account.';return;}
    const query=new URLSearchParams({facility_id:$('facility').value,date:$('visit-date').value});
    ['doctor','service','specialty'].forEach(id=>{if($(id).value)query.set(id+'_id',$(id).value);});
    try {
      let url='/api/v1/availability/?'+query, slots=[];
      while(url){ const data=await api(url);slots.push(...data.results);url=data.next;if(requestId!==loadId)return;}
      lastSuccess=Date.now();$('freshness').textContent='Live availability · Updated '+new Date().toLocaleTimeString()+'. Times shown in clinic time.';
      $('freshness').className='panel-note mt-3';$('slots').replaceChildren();
      slots.forEach(slot=>{
        const button=node('button',null,'slot-choice');button.type='button';
        button.append(node('strong',localTime(slot.starts_at)),node('span',slot.doctor),node('small',slot.service+' · LKR '+slot.fee_preview.total),node('small',slot.status==='available'?'Reserve time':slot.status==='held'?'Temporarily held':'Booked'));
        button.disabled=slot.status!=='available'||busy||!!activeHold;
        button.addEventListener('click',()=>reserve(slot));$('slots').append(button);
      });
      if(!slots.length)$('slots').append(node('p','No appointment times are published for these filters. Try another date or doctor.','subtle'));
    } catch(e) {
      if(requestId!==loadId)return;
      $('freshness').textContent='Availability is stale. Could not refresh; retry Find available times.';
      $('freshness').className='alert alert-warning mt-3';
      $('slots').querySelectorAll('button').forEach(b=>b.disabled=true);
    }
  }
  $('earliest').addEventListener('click',async()=>{
    if(busy||activeHold)return;
    clearError();const query=new URLSearchParams({facility_id:$('facility').value});
    ['doctor','service','specialty'].forEach(id=>{if($(id).value)query.set(id+'_id',$(id).value);});
    try{const data=await api('/api/v1/availability/?'+query);
      if(!data.results.length){error('No available times are published in the release window for these filters.');return;}
      $('visit-date').value=data.results[0].local_date;load();
    }catch(e){error(e.message);}
  });
  function holdState() {
    $('earliest').disabled=!!activeHold;
    $('confirmation').hidden=!activeHold;$('review-empty').hidden=!!activeHold;
    $('filters').querySelectorAll('input,select,button').forEach(el=>el.disabled=!!activeHold);
    if($('patient-search'))document.querySelectorAll('#patient-search input,#patient-search button,#new-patient input,#new-patient button,#patient-results button').forEach(el=>el.disabled=!!activeHold);
  }
  async function reserve(slot) {
    if(busy||activeHold)return;
    clearError();
    if(config.role==='reception'&&!selectedPatient){error('Select or create a patient before reserving a time.');return;}
    if(config.role==='patient'&&!config.verified){error('Verify your email before booking.');return;}
    busy=true;$('booking-success').hidden=true;
    try {
      const body={slot_id:slot.id,expected_version:slot.version};if(selectedPatient)body.patient_id=selectedPatient.id;
      activeHold=await api('/api/v1/holds/','POST',body);chosenSlot=slot;
      $('hold-summary').replaceChildren(node('h3',slot.doctor),node('p',slot.service+' · '+$('visit-date').value+' at '+localTime(slot.starts_at)),node('p','LKR '+activeHold.fee_preview.total+' · Due at counter'));
      $('consent').checked=false;holdState();$('reason').focus();
    }catch(e){error(e.message);}finally{busy=false;load();}
  }
  $('confirmation').addEventListener('submit',async e=>{
    e.preventDefault();if(busy||!activeHold)return;busy=true;clearError();$('confirm-button').disabled=true;
    try {
      const result=await api('/api/v1/appointments/','POST',{hold_id:activeHold.id,expected_version:activeHold.version,payment_method:'counter_due',consent_version:'demo-v1',reason_category:$('reason').value});
      activeHold=null;holdState();$('review-empty').hidden=true;
      const link=node('a','View confirmation','btn btn-care');link.href='/appointments/'+result.id+'/';
      $('booking-success').replaceChildren(node('h3','Appointment confirmed'),node('p',result.reference),node('p','LKR '+result.snapshot.total+' is due at the clinic counter.'),link);$('booking-success').hidden=false;link.focus();
    }catch(e){error(e.message);if(e.status===410){activeHold=null;holdState();}}finally{busy=false;$('confirm-button').disabled=false;load();}
  });
  $('release-button').addEventListener('click',async()=>{
    if(busy||!activeHold)return;busy=true;clearError();
    try{await api('/api/v1/holds/'+activeHold.id+'/','DELETE',{expected_version:activeHold.version});activeHold=null;holdState();}
    catch(e){error(e.message);}finally{busy=false;load();}
  });
  if(config.role==='reception') {
    $('reference-search').addEventListener('submit',async e=>{
      e.preventDefault();clearError();
      try{const data=await api('/api/v1/appointments/?q='+encodeURIComponent($('booking-reference').value));
        $('reference-results').replaceChildren();
        if(!data.results.length)$('reference-results').append(node('p','No booking found in your scope.'));
        data.results.forEach(a=>{const card=node('article',null,'mb-3');
          card.append(node('strong',a.reference),node('p',a.patient_name+' · '+a.snapshot.doctor),
          node('p',new Date(a.starts_at).toLocaleString([],{timeZone:a.snapshot.timezone})+' · '+a.snapshot.timezone),
          node('p',a.state+' · LKR '+a.snapshot.total+' due at counter'));
          $('reference-results').append(card);
        });
      }catch(err){error(err.message);}
    });
    const selectPatient = p=>{selectedPatient=p;$('selected-patient').textContent='Selected: '+p.name+(p.possible_duplicate?' · Another patient has this name. Verify identity; no records were merged.':'');};
    $('patient-search').addEventListener('submit',async e=>{
      e.preventDefault();clearError();
      try{const data=await api('/api/v1/patients/?q='+encodeURIComponent($('patient-query').value)+'&facility_id='+encodeURIComponent($('facility').value));$('patient-results').replaceChildren();
        const matches=data.results.filter(p=>String(p.facility)===$('facility').value);
        if(!matches.length)$('patient-results').append(node('p','No matching patients. Search by exact email or create a new patient.'));
        matches.forEach(p=>{const b=node('button',p.name+' · '+(p.email||p.phone||'No contact'),'btn btn-outline-dark w-100 mb-2');b.type='button';b.addEventListener('click',()=>selectPatient(p));$('patient-results').append(b);});
        if(data.next)$('patient-results').append(node('p','More matches exist. Narrow your search using the patient’s exact email.'));
        if(matches.length>1)$('patient-results').append(node('p','Multiple matches: confirm the patient’s identity. Records are not merged.','panel-note'));
      }catch(err){error(err.message);}
    });
    $('new-patient').addEventListener('submit',async e=>{
      e.preventDefault();if(busy)return;busy=true;clearError();
      try{const p=await api('/api/v1/patients/','POST',{facility:Number($('facility').value),name:$('patient-name').value,email:$('patient-email').value,phone:$('patient-phone').value});selectPatient(p);$('new-patient').reset();}
      catch(err){error(err.message);}finally{busy=false;}
    });
  }
  setInterval(()=>{if(!busy&&!document.hidden)load();},15000);
  setInterval(()=>{
    if(activeHold){const seconds=Math.max(0,Math.ceil((new Date(activeHold.expires_at)-Date.now())/1000));$('countdown').textContent=seconds?'Held for '+Math.floor(seconds/60)+':'+String(seconds%60).padStart(2,'0'):'Hold expired. Release and choose another time.';$('confirm-button').disabled=busy||!seconds;}
    if(lastSuccess&&Date.now()-lastSuccess>30000){$('freshness').textContent='Availability is stale. Refresh before choosing a time.';$('slots').querySelectorAll('button').forEach(b=>b.disabled=true);}
  },1000);
  load();
})();
