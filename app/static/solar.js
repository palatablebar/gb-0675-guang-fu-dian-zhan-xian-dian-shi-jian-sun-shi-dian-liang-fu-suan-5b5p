const solar={busy:false};const status=document.querySelector('#feedback');
const datasetPicker=document.querySelector('#dataset');const currentRun=document.body.dataset.runId;const currentSource=document.body.dataset.source;
document.querySelector('#recompute').addEventListener('click',async()=>{if(solar.busy)return;solar.busy=true;const previous=datasetPicker.value;status.textContent='复算中…';try{
 const dataset=datasetPicker.value;
 // 同源重跑走不可变快照，已复核批次会被原样复用，不新增、不覆盖。
 const response=await fetch(dataset===currentSource?'/api/runs/'+currentRun+'/rerun':'/api/runs',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({dataset})});
 const run=await response.json();
 if(!response.ok)throw Error(run.detail||'切换失败');
 if(!run.manifest||!run.manifest.run_id||run.source!==dataset)throw Error('返回批次与所选样例不一致');
 const url=new URL(location.href);url.searchParams.set('run_id',run.manifest.run_id);url.searchParams.set('dataset',dataset);location.href=url;
}catch(error){
 // 切换失败：留在当前批次，选择框恢复旧值，不覆盖页面上的旧版结果。
 datasetPicker.value=currentSource;status.textContent=error.message+'，已保留当前批次 '+currentRun;
}finally{solar.busy=false;}});
datasetPicker.addEventListener('change',()=>{status.textContent='';});
const filePicker=document.querySelector('#csv-file');
if(filePicker){
 const editor=document.querySelector('#csv-text');
 filePicker.addEventListener('change',async()=>{const file=filePicker.files[0];if(!file)return;if(file.size>2000000){editor.value='';alert('文件超过2MB，请缩小后导入');return;}const bytes=await file.arrayBuffer();try{editor.value=new TextDecoder('utf-8',{fatal:true}).decode(bytes);}catch(e){editor.value=new TextDecoder('gb18030',{fatal:true}).decode(bytes);}});
 document.querySelector('#import-csv').addEventListener('click',async event=>{event.target.disabled=true;try{const response=await fetch('/api/runs',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({csv_text:editor.value})});const data=await response.json();if(!response.ok)throw Error(data.detail);if(!data.manifest||!data.manifest.run_id)throw Error('返回批次无效');location.href=location.pathname+'?run_id='+data.manifest.run_id;}catch(e){editor.setCustomValidity(e.message);editor.reportValidity();}finally{event.target.disabled=false;}});
 editor.addEventListener('input',()=>editor.setCustomValidity(''));
}
