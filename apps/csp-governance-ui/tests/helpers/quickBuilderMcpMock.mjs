export const calls=[]
export const requestOptions=[]
export function reset(){calls.length=0;requestOptions.length=0}
const service={id:5,name:'差勤服務',description:'只查本人',endpoint_url:'https://mcp.internal/mcp',department_id:2,status:'pending',tools:[{id:8,name:'查假',description:'查詢假別餘額',input_schema:{type:'object',properties:{month:{type:'integer'}}},annotations:{readOnlyHint:true},schema_fingerprint:'abc',approved_fingerprint:null,is_read_only:false,is_approved:false}]}
export default {get:async(path)=>path.endsWith('/catalog') ? {data:{actor_user_id:101}} : {data:{services:[structuredClone(service)]}},post:async(path,body,options)=>{requestOptions.push(options);calls.push({path,body});return{data:{}}}}
