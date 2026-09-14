export function NotFoundPage() {
  return (
    <div style={{ minHeight: '100dvh', display: 'grid', placeItems: 'center', padding: 24, textAlign: 'center' }}>
      <div>
        <div style={{ fontSize: 13, letterSpacing: '0.12em', opacity: 0.6 }}>ANILA LM</div>
        <h1 style={{ fontSize: 22, margin: '10px 0 8px' }}>頁面不存在</h1>
        <p style={{ opacity: 0.7, margin: '0 0 16px' }}>你要前往的知識庫路徑不存在。</p>
        <a href="./" style={{ color: '#2b4c7e' }}>回到知識庫</a>
      </div>
    </div>
  )
}
