export const calls = []
export const mutationRequests = []
const clone = v => structuredClone(v)
const graph = {nodes:[{id:'in',kind:'input',label:'輸入',position:{x:20,y:30},config:{placeholder:'請輸入問題'}},{id:'condition',kind:'condition',label:'判斷',position:{x:240,y:30},config:{field:'text',operator:'contains',value:'請假'}},{id:'yes',kind:'output',label:'成立輸出',position:{x:460,y:30},config:{include_sources:true}},{id:'no',kind:'output',label:'不成立輸出',position:{x:460,y:190},config:{include_sources:true}}],edges:[{source:'in',target:'condition'},{source:'condition',target:'yes',branch:'true'},{source:'condition',target:'no',branch:'false'}]}
let detail, failNext,manageTemplates,actorId,holdRead,releaseRead,identityReject
export function reset(){ calls.length=0;mutationRequests.length=0;failNext=false;manageTemplates=false;actorId=101;holdRead=false;releaseRead=undefined;identityReject=false;detail={id:11,name:'規章問答',draft_revision:2,can_edit:true,can_review:true,disabled:false,versions:[{id:22,revision:2,status:'draft',name:'規章問答',description:'單位規章問答',model_id:1,classification_level:'無機密',collection_ids:[],scope:'unit',target_department_ids:[],graph:clone(graph)},{id:21,revision:1,status:'approved',name:'舊版規章',description:'舊說明',model_id:1,classification_level:'無機密',collection_ids:[],scope:'unit',target_department_ids:[],graph:clone(graph)}]}}
reset()
export function changeActor(id){actorId=id}
export function rejectIdentity(){identityReject=true}
export function holdDetail(){holdRead=true}
export function releaseDetail(){holdRead=false;releaseRead?.()}
function recordMutation(kind,options){mutationRequests.push({kind,options:clone(options)});if(identityReject || options?.headers?.['X-ANILA-Actor-Id'] !== String(actorId)){const e={code:'ERR_BAD_REQUEST',response:{status:409,data:{detail:{type:'IDENTITY_CHANGED',message:'登入身分已變更'}}}};throw e}}
export function setManageTemplates(){manageTemplates=true}
export function setFailure(){failNext=true}
export function setPending(){detail.versions[0].status='pending'}
export const listAssistants=async()=>({data:{assistants:[{id:11,name:detail.name}]}})
export const getAssistant=async id=>{if(holdRead)await new Promise(resolve=>releaseRead=resolve);calls.push(['get',id]);return {data:clone(detail)}}
export const listTemplates=async()=>({data:{templates:[{id:'rules',name:'規章問答範本',description:'查詢文件',graph:clone(graph),configuration_notes:'請選擇文件集'}]}})
export const getCatalog=async()=>({data:{actor_user_id:actorId,can_manage_templates:manageTemplates,models:[{id:1,name:'平台核准模型'}],collections:[{id:4,name:'採購規章',publication_scope:'unit',target_department_ids:[]}],mcp_tools:[{id:8,name:'請假餘額',description:'查本人餘額',input_schema:{type:'object',required:['flag'],properties:{flag:{type:'boolean',default:false},month:{type:'integer',description:'查詢月份'}}}}]}})
export const createAssistant=async(body,options)=>{recordMutation('create',options);calls.push(['create',clone(body)]);detail.versions[0]={...clone(body),id:22,revision:2,status:'draft'};return{data:clone(detail)}}
export const saveVersion=async(id,body,options)=>{recordMutation('save',options);calls.push(['save',id,clone(body)]);if(failNext){failNext=false;throw{response:{status:409,data:{detail:{type:'REVISION_CONFLICT',message:'版本已由另一個視窗更新'}}}}}detail.draft_revision++;detail.versions.unshift({...clone(body),id:23,revision:detail.draft_revision,status:'draft'});return{data:clone(detail)}}
export const assistantAction=async(id,action,body,options)=>{recordMutation(action,options);calls.push([action,id,clone(body)]);if(action==='test')return{data:{status:'completed',output:'測試回答',artifacts:[{filename:'草稿.rtf',download_url:'/api/quick-builder/runs/1/artifacts/0'},{filename:'不安全鏈結',download_url:'javascript:alert(1)'}],steps:[{node_id:'condition',elapsed_ms:18,input:{password:'never-display',question:'Bearer private-token'},output:{text:'可請假'},retrieval:[{filename:'規章.pdf',page:3,text:'核定條文'}],tool:{arguments:{api_key:'private-key',month:10},result:'餘額3天'}}]}};if(action==='restore'){detail.draft_revision++;detail.versions.unshift({...clone(detail.versions.find(v=>v.id===body.version_id)),id:24,revision:detail.draft_revision,status:'draft'})}if(action==='submit')detail.versions.find(v=>v.id===body.version_id).status='pending';if(action==='review')detail.versions.find(v=>v.id===body.version_id).status=body.approve?'approved':'rejected';return{data:clone(detail)}}
export const compareVersions=async(id,left,right)=>{calls.push(['compare',id,left,right]);return{data:{changes:{name:{left:'舊版規章',right:'規章問答'}}}}}

export const createTemplate=async(body,options)=>{recordMutation('createTemplate',options);calls.push(['createTemplate',clone(body)]);return{data:{id:3}}}
export const updateTemplate=async(id,body,options)=>{recordMutation('updateTemplate',options);calls.push(['updateTemplate',id,clone(body)]);return{data:{id}}}
export const setCollectionPublication=async(id,body,options)=>{recordMutation('collectionPublication',options);calls.push(['collectionPublication',id,clone(body)]);return{data:{}}}
