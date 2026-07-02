// 與 backend schemas/studio.py 的 THEMES list 對應
// 顯示用元資料：id 是給 API、name+description 是給 UI、swatches 是預覽縮圖用

export type ThemeId =
  | 'auto'                  // 不指定，讓系統照舊邏輯（Patch O tone + Patch U title-override）
  | 'corporate_navy'
  | 'academic_paper'
  | 'warm_journal'
  | 'executive_brief'
  | 'startup_pitch'

export interface ThemeDescriptor {
  id: ThemeId
  name: string                // 「企業技術」「個人心得」等
  description: string         // 一句話定位
  suitableFor: string[]       // 三個 tag：「給同事」「給主管」等
  swatches: {
    bar: string               // 主色 / 標題列
    accent: string            // 強調色
    ink: string               // 文字色
    bg: string                // 背景色
  }
  typography: 'sans' | 'serif'
  iconStyle: 'outlined-circle' | 'soft-filled' | 'monochrome-dot' | 'filled-pill' | 'minimal-dot'
}

export const THEMES: ThemeDescriptor[] = [
  {
    id: 'auto',
    name: '自動偵測',
    description: '依據文件 title 與內容自動挑選風格',
    suitableFor: ['不知道哪個適合', '快速生成', '相信系統判斷'],
    swatches: { bar: '#888', accent: '#aaa', ink: '#222', bg: '#fff' },
    typography: 'sans',
    iconStyle: 'outlined-circle',
  },
  {
    id: 'corporate_navy',
    name: '企業技術',
    description: '深藍配琥珀，給同事或主管的技術 / 業務報告',
    suitableFor: ['給同事', '給主管', '技術深度報告'],
    swatches: { bar: '#1E2761', accent: '#F4B740', ink: '#1A1A1A', bg: '#FFFFFF' },
    typography: 'sans',
    iconStyle: 'outlined-circle',
  },
  {
    id: 'academic_paper',
    name: '學術研究',
    description: '炭灰配 serif 字型，學術發表 / 深度研究風',
    suitableFor: ['研討會', '研究報告', '基本面分析'],
    swatches: { bar: '#FFFFFF', accent: '#A0826D', ink: '#212121', bg: '#FFFFFF' },
    typography: 'serif',
    iconStyle: 'monochrome-dot',
  },
  {
    id: 'warm_journal',
    name: '個人心得',
    description: '米白配棕褐，第一人稱回顧 / 學習紀錄',
    suitableFor: ['個人心得', '反思記錄', '軟性分享'],
    swatches: { bar: '#FFF8F0', accent: '#D2691E', ink: '#4A3429', bg: '#FFFCF7' },
    typography: 'sans',
    iconStyle: 'soft-filled',
  },
  {
    id: 'executive_brief',
    name: '高層 Briefing',
    description: '黑白極簡無 chrome，結論導向 ≤ 10 張',
    suitableFor: ['給 C-level', '極簡', '結論導向'],
    swatches: { bar: '#FFFFFF', accent: '#1A1A1A', ink: '#1A1A1A', bg: '#FFFFFF' },
    typography: 'sans',
    iconStyle: 'minimal-dot',
  },
  {
    id: 'startup_pitch',
    name: '對外發表',
    description: '巨型字級 + 高彩度，對客戶 / 投資人 / 產品發表',
    suitableFor: ['對外發表', '投資人', '產品介紹'],
    swatches: { bar: '#2F3C7E', accent: '#F96167', ink: '#1A1A1A', bg: '#FFFFFF' },
    typography: 'sans',
    iconStyle: 'filled-pill',
  },
]

export function findTheme(id: ThemeId): ThemeDescriptor {
  return THEMES.find((t) => t.id === id) ?? THEMES[0]
}
