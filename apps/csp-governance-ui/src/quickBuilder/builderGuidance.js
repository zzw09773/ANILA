import { moduleGuide } from './moduleGuides.js';

export function setupNeeds(form) {
  const needs = [];
  if (!form.name?.trim()) needs.push({key: 'name', message: '填寫助手名稱，方便同仁辨識用途。'});
  if (!form.model_id) needs.push({key: 'model', message: '選擇平台模型，供助手整理與回答問題。'});
  const nodes = form.graph?.nodes || [], edges = form.graph?.edges || [];
  for (const node of nodes) {
    const add = message => needs.push({key: node.id, nodeId: node.id, message: `${node.label}：${message}`});
    const c = node.config || {};
    if (node.kind === 'rag' && !c.collection_ids?.length) add('選擇要搜尋的文件集。');
    if (node.kind === 'mcp' && !c.tool_id) add('選擇已核准的查詢工具並填寫查詢條件。');
    if (node.kind === 'agent' && !c.tool_ids?.length) add('勾選至少一個允許查詢的核准工具。');
    if (node.kind === 'prompt' && !c.instruction?.trim()) add('填寫這個步驟要完成的工作。');
    if (node.kind === 'extract' && !c.fields?.length) add('指定至少一個要整理的欄位。');
    if (node.kind === 'condition' && c.operator !== 'is_empty' && (c.value === '' || c.value == null)) add('填寫要判斷的內容，例如「請假」。');
    if (node.kind === 'document' && !(c.title || '').trim()) add('填寫文件標題。');
    if (node.kind === 'document' && c.mode !== 'studio' && !(c.template || '').trim()) add('填寫文件文字範本，才能製作文字文件草稿。');
    if (node.kind === 'condition') {
      for (const branch of ['true', 'false']) if (!edges.some(e => e.source === node.id && e.branch === branch)) add(`將「${branch === 'true' ? '成立' : '不成立'}」出口連到下一步。`);
    } else if (node.kind !== 'output' && !edges.some(e => e.source === node.id)) add('連到下一步，讓處理結果可以繼續傳遞。');
    if (node.kind !== 'input' && !edges.some(e => e.target === node.id)) add('接上前一步，這個步驟才會收到資料。');
  }
  return needs;
}

export function nodeSummary(node) {
  const c = node.config || {};
  if (node.kind === 'rag') return c.collection_ids?.length ? `已選 ${c.collection_ids.length} 個文件集` : '尚未選擇文件集';
  if (node.kind === 'mcp') return c.tool_id ? '已選工具，請確認查詢條件' : '尚未選擇查詢工具';
  if (node.kind === 'agent') return `可使用 ${c.tool_ids?.length || 0} 個核准工具`;
  if (node.kind === 'condition') return '依成立／不成立，走不同處理路徑';
  return moduleGuide(node.kind).description;
}

export function nodeIO(kind) {
  const pair = ({input:['同仁問題','問題文字'],output:['處理結果','對話回覆'],prompt:['工作要求','回答指示'],model:['問題與資料','回答文字'],rag:['問題與文件','相關依據'],mcp:['查詢條件','查詢結果'],condition:['資料','成立／不成立'],extract:['文字','指定欄位'],format:['原內容','整理文字'],document:['內容與範本','文件草稿'],agent:['問題與工具','整理結果']})[kind];
  return pair ? {input:pair[0],output:pair[1]} : {input:'未提供',output:'未提供'};
}

export function stepSummary(step, kind) {
  if (step.status === 'incomplete') return '已收到部分結果，但回答尚未完整。';
  if (step.error) return '這個步驟未能完成；請依下方說明處理。';
  return ({input:'已接收這次問題。',prompt:'已準備工作要求，交給後續步驟。',model:'已產生回答內容。',rag:'已依目前使用者權限查找文件。',mcp:'已用提問者身分查詢核准工具。',condition:'已依條件選擇後續路徑。',extract:'已將資料整理為指定欄位。',format:'已整理文字的呈現方式。',document:'已完成文件處理，請確認是否有可下載草稿。',agent:'已完成規劃與核准工具的處理。',output:'已將結果交付為助手回覆。'})[kind] || '已收到這個步驟的執行紀錄。';
}

export function resultText(value) {
  if (typeof value === 'string') return value;
  if (typeof value?.text === 'string') return value.text;
  return value == null ? '' : '這次回覆包含結構化資料，請展開步驟詳細紀錄確認內容。';
}
