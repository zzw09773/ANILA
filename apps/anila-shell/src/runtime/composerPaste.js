// 輸入框貼上：有圖就當附件，有真正的文字就留給瀏覽器貼字。
//
// 從網頁「複製圖片」常同時帶 text/html（<img>）與 image/png。舊邏輯把任何
// text/* 都當成文字，結果圖貼不進去。反過來，從網頁圈選文字時剪貼簿也常
// 帶一張選取範圍截圖——那種必須貼文字，不能偷換成附件。

const IMAGE_NAME = /\.(png|jpe?g|gif|webp|bmp|svg)$/i;

export function isClipboardFilenameOnly(text) {
  const t = String(text || "").trim();
  if (!t) return true;
  if (/\r|\n/.test(t)) return false;
  if (IMAGE_NAME.test(t)) return true;
  return false;
}

export function namePastedFile(file) {
  if (!file) return null;
  const stamp = new Date().toISOString().replace(/[:.]/g, "-").slice(0, 19);
  const ext = ((file.type || "").split("/")[1] || "bin").split("+")[0];
  if (file.name && file.name !== "image.png") return file;
  return new File([file], `貼上-${stamp}.${ext}`, { type: file.type });
}

function pushUniqueFile(files, seen, file) {
  const named = namePastedFile(file);
  if (!named) return;
  const key = `${named.name}\0${named.size}\0${named.type}`;
  if (seen.has(key)) return;
  seen.add(key);
  files.push(named);
}

export function filesFromComposerClipboard(clipboardData) {
  if (!clipboardData) return [];
  const files = [];
  const seen = new Set();
  const items = clipboardData.items;
  if (items) {
    for (let i = 0; i < items.length; i += 1) {
      const it = items[i];
      if (it && it.kind === "file" && typeof it.getAsFile === "function") {
        pushUniqueFile(files, seen, it.getAsFile());
      }
    }
  }
  if (!files.length && clipboardData.files) {
    for (let i = 0; i < clipboardData.files.length; i += 1) {
      pushUniqueFile(files, seen, clipboardData.files[i]);
    }
  }
  if (!files.length) return [];
  const plain = typeof clipboardData.getData === "function"
    ? clipboardData.getData("text/plain")
    : "";
  if (!isClipboardFilenameOnly(plain)) return [];
  return files;
}
