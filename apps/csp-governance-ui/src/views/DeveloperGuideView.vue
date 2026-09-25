<template>
  <div class="page">
    <header class="page-head">
      <div>
        <h1 class="page-head__title">打造助手</h1>
        <p class="page-head__sub">
          預設路徑是快速起步。到
          <router-link to="/developer/agents">助手</router-link>
          下載通用包，在 MLSteam lab 只改 <code>agent.py</code>，再回來註冊。
        </p>
      </div>
    </header>

    <TermBox title="預設路徑 · 快速起步" pad="md">
      <p class="lead">
        下載的 zip 根目錄是 <code>anila-agent-quickstart/</code>。不必先註冊：註冊要 endpoint，而那個位址要等服務起來、做好 port forwarding 才存在。
        lab 由平台建好的映像開成一台長期開著的 Linux 虛擬機，裡面沒有 Docker。zip 只有程式與站台設定，沒有 API 金鑰。
      </p>
      <ol class="steps">
        <li>
          到 <router-link to="/developer/agents">助手</router-link> 按「下載快速起步」，取得通用 zip。
          用與 <code>bundle.json</code> 的 <code>compatible_lab_image_version</code> 相同的 lab 映像開一個 lab，把裡面的檔案放到工作目錄 <code>/app</code>。
          <code>ANILA_CA_FILE</code> 已指到 <code>/app/ca.pem</code>。
        </li>
        <li>
          在 JupyterLab 只改 <code>agent.py</code> 的回答。<code>COLLECTION_ID</code> 選填。
          範例 <code>AGENT_NAME</code> 可以留著；若要改，改完再用同一個名字註冊。註冊之後不要再改。
        </li>
        <li>
          在 <code>deployment.env</code> 填 <code>LLM_MODEL</code>（你在 Console 獲准使用的模型名稱）。平台不指定模型。
          金鑰只放環境，不要寫進這個檔、也不要提交：
          <pre class="code">export LLM_API_KEY=…</pre>
          這是你自己的金鑰。<code>LLM_BASE_URL</code> 已是 CSP 的 <code>/v1</code>，權限、用量與稽核都留在 CSP。維持下載包裡的這個值。
        </li>
        <li><code>./run.sh start</code>。服務聽埠 8200。</li>
        <li>在 MLSteam 把 port forwarding 指到 8200。</li>
        <li>
          回到 Console 註冊助手。endpoint 填轉出去的 http 位址。
          註冊要填名稱、至少 24 字的用途說明、endpoint，以及基礎模型。
        </li>
        <li>
          把註冊得到的數字 id 填進 <code>deployment.env</code> 的 <code>ANILA_AGENT_ID</code>，執行 <code>./run.sh restart</code>。
        </li>
      </ol>
      <p class="hint">
        <code>GET /health</code> 回 200 才表示可以接派工，不代表模型答得通。回到 Console 完成核准，再在對話裡選這個助手。
        <code>./run.sh status</code> 會印出跟 <code>/health</code> 相同的 <code>reason</code>。
        查看用 <code>./run.sh status</code>、<code>./run.sh logs</code>；停機用 <code>./run.sh stop</code>。
      </p>
    </TermBox>

    <TermBox title="健康檢查先看 reason" pad="md">
      <table class="term-table">
        <thead><tr><th style="width: 220px">reason</th><th>下一步</th></tr></thead>
        <tbody>
          <tr>
            <td>啟動被拒，缺 CSP_BASE_URL／ANILA_CA_FILE／LLM_BASE_URL</td>
            <td>用下載包裡的 <code>deployment.env</code>，不要手填站台位址。</td>
          </tr>
          <tr>
            <td>啟動被拒，映像版本不符</td>
            <td>換 <code>bundle.json</code> 寫的那版 lab 映像，或重新下載 zip。</td>
          </tr>
          <tr>
            <td><code>not_registered</code></td>
            <td>註冊後把數字 id 填進 <code>ANILA_AGENT_ID</code>，再 <code>./run.sh restart</code>。</td>
          </tr>
          <tr>
            <td><code>llm_not_configured</code></td>
            <td>填 <code>LLM_MODEL</code>，並在 lab <code>export LLM_API_KEY</code> 後重啟。</td>
          </tr>
          <tr>
            <td><code>jwks_unavailable</code></td>
            <td>確認 <code>CSP_BASE_URL</code> 與 <code>ca.pem</code>；不要關 TLS。</td>
          </tr>
          <tr>
            <td><code>upstream_error</code></td>
            <td>確認模型名稱是你獲准使用的，且金鑰仍有效。</td>
          </tr>
          <tr>
            <td><code>search_failed</code></td>
            <td>知識庫未綁定、派工憑條過期，或搜尋被拒。</td>
          </tr>
        </tbody>
      </table>
      <p class="hint">若模型和金鑰都還沒設，<code>/health</code> 先報 <code>llm_not_configured</code>，補上之後才報 <code>not_registered</code>。</p>
    </TermBox>

    <TermBox title="知識庫" pad="md">
      <p class="lead">
        知識庫在 Console 綁定，而且必須屬於這位助手的擁有者。
        <code>agent.py</code> 的 <code>COLLECTION_ID</code> 只是挑選已經綁定的其中一個；沒有要查就留 <code>None</code>。
        它不授予權限。搜尋仍由平台依擁有者與已綁定的集合檢查。
      </p>
    </TermBox>

    <TermBox title="進階實作範例" pad="sm">
      <p class="lead">
        助手頁的第二個下載是「下載進階範例」，對應 <code>packages/anila-agent</code>（zip 根目錄 <code>anila-agent-advanced-example/</code>）。
        要做工具迴圈、長期記憶、多輪或背景工作時才用。它本身就是服務，也不是快速起步的下一章。
      </p>
      <p class="hint">
        既有 Python 服務若要自己接驗簽，仍用助手頁的「下載 anila_verify.py」。快速起步 zip 裡已經有同一支檔與 <code>ca.pem</code>。
      </p>
    </TermBox>
  </div>
</template>

<script setup>
import { TermBox } from '../components/cli'
</script>

<style scoped>
.page { display: flex; flex-direction: column; gap: var(--gap-4); }
.page-head { display: flex; align-items: flex-start; justify-content: space-between; gap: var(--gap-4); }
.page-head__title { font-size: var(--t-xl); font-weight: 500; color: var(--c-fg-1); margin: 0 0 4px; }
.page-head__sub { color: var(--c-fg-2); font-size: var(--t-sm); margin: 0; }

.lead { font-size: var(--t-sm); color: var(--c-fg-2); margin: 0 0 var(--gap-3); }
.lead strong { color: var(--c-fg-1); }

.steps {
  list-style: decimal inside; padding: 0; margin: 0;
  display: flex; flex-direction: column; gap: var(--gap-2);
  font-size: var(--t-sm); color: var(--c-fg-2);
}
.steps strong { color: var(--c-fg-1); }

.steps code, .lead code, .hint code, .page-head__sub code, .term-table code {
  font-family: var(--font-mono); background: var(--c-bg);
  border: var(--border-w) solid var(--c-border); padding: 1px 4px;
  font-size: var(--t-2xs); color: var(--c-accent);
}

.code {
  margin: var(--gap-2) 0; padding: var(--gap-3);
  background: var(--c-bg); border: var(--border-w) solid var(--c-border);
  font-family: var(--font-mono); font-size: var(--t-2xs);
  color: var(--c-fg-1); white-space: pre; overflow-x: auto;
  line-height: 1.55;
}

.hint {
  font-size: var(--t-xs); color: var(--c-fg-3); margin: var(--gap-3) 0 0;
  font-style: italic;
}
.hint strong { color: var(--c-fg-2); font-style: normal; }

.page-head__sub a, .steps a, .lead a { color: var(--c-accent); text-decoration: none; }
.page-head__sub a:hover, .steps a:hover, .lead a:hover { text-decoration: underline; }

.term-table { width: 100%; }
</style>
