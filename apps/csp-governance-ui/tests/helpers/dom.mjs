// 先於 `vue` 被 import：ES import 會提升，GlobalRegistrator 若寫在測試檔本體，
// runtime-dom 已經在沒有 document 的環境下載入了。
import { GlobalRegistrator } from '@happy-dom/global-registrator'

GlobalRegistrator.register()
