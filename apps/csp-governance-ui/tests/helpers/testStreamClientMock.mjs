export const requests=[];
let next;
export function respond(value){next=value;requests.length=0;}
export default {async post(url,body,options){requests.push({url,body,options});if(next instanceof Error)throw next;return next;}};
