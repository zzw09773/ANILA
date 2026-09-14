export default function NotFound() {
  return (
    <div style={{
      minHeight: "100dvh",
      display: "grid",
      placeItems: "center",
      padding: 24,
      background: "var(--bg, #f7f8fa)",
      color: "var(--fg, #1b2230)",
      fontFamily: "inherit",
      textAlign: "center",
    }}>
      <div>
        <div style={{ fontSize: 13, letterSpacing: "0.12em", color: "var(--fg-muted, #686f7e)" }}>ANILA</div>
        <h1 style={{ fontSize: 22, margin: "10px 0 8px" }}>頁面不存在</h1>
        <p style={{ color: "var(--fg-muted, #686f7e)", margin: "0 0 16px" }}>你要前往的路徑不在對話平台裡。</p>
        <a href="./app" style={{ color: "var(--accent, #2b4c7e)" }}>回到對話</a>
      </div>
    </div>
  );
}
