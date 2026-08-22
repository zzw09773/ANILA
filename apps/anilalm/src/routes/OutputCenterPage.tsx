import { useCallback, useEffect, useState } from 'react'
import { useNavigate } from 'react-router'
import { listCollections } from '../api/collections'
import { explainError } from '../api/client'
import { loginHref } from '../appOrigins'
import { Icon } from '../components/Icon'
import { ProductHeader } from '../components/ProductHeader'
import { Spinner } from '../components/Spinner'
import { useAuthStore } from '../store/auth'
import { useTheme } from '../theme/ThemeContext'
import type { Collection } from '../types'

export function OutputCenterPage() {
  const { t } = useTheme()
  const navigate = useNavigate()
  const user = useAuthStore((s) => s.user)
  const logout = useAuthStore((s) => s.logout)
  const [collections, setCollections] = useState<Collection[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  const load = useCallback(async () => {
    setError(null)
    setLoading(true)
    try {
      const { data } = await listCollections({ owned_only: true, include_archived: false })
      setCollections(data)
    } catch (err) {
      setError(explainError(err))
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    void load()
  }, [load])

  const handleLogout = async () => {
    await logout()
    window.location.replace(loginHref())
  }

  return (
    <div
      style={{
        minHeight: '100dvh',
        background: t.bg,
        color: t.text,
        display: 'flex',
        flexDirection: 'column',
      }}
    >
      <ProductHeader
        active="outputs"
        user={user}
        onLogout={() => void handleLogout()}
      />

      <main style={{ flex: 1, padding: '40px 64px', overflow: 'auto' }}>
        <div style={{ maxWidth: 1040, margin: '0 auto' }}>
          <p style={{ margin: '0 0 8px', color: t.accent, fontSize: 12, fontWeight: 600 }}>
            產出中心
          </p>
          <h1 style={{ margin: 0, fontSize: 32, fontWeight: 650, letterSpacing: -0.7 }}>
            從知識庫製作成果
          </h1>
          <p style={{ margin: '8px 0 30px', color: t.textMuted, fontSize: 14, lineHeight: 1.7 }}>
            選擇已有索引資料的知識庫，製作報告、簡報、心智圖、資訊圖或資料表。
          </p>

          {error && (
            <div
              role="alert"
              style={{
                marginBottom: 18,
                padding: '10px 14px',
                borderRadius: 8,
                color: t.danger,
                background: `${t.danger}14`,
                border: `1px solid ${t.danger}33`,
              }}
            >
              {error}
              <button
                type="button"
                onClick={() => void load()}
                style={{
                  marginLeft: 12,
                  border: 0,
                  background: 'transparent',
                  color: t.danger,
                  cursor: 'pointer',
                }}
              >
                重試
              </button>
            </div>
          )}

          {loading ? (
            <div style={{ padding: 64, textAlign: 'center', color: t.textMuted }}>
              <Spinner /> 載入知識庫中…
            </div>
          ) : collections.length === 0 ? (
            <div
              style={{
                padding: 36,
                background: t.surface,
                border: `1px solid ${t.border}`,
                borderRadius: 10,
                textAlign: 'center',
              }}
            >
              <strong>還沒有可用的知識庫</strong>
              <p style={{ color: t.textMuted, fontSize: 13 }}>
                請先到「我的知識庫」建立知識庫並上傳資料。
              </p>
              <button
                type="button"
                onClick={() => navigate('/')}
                style={{
                  padding: '8px 14px',
                  borderRadius: 7,
                  border: 0,
                  background: t.accent,
                  color: '#fff',
                  cursor: 'pointer',
                }}
              >
                前往我的知識庫
              </button>
            </div>
          ) : (
            <div
              style={{
                display: 'grid',
                gridTemplateColumns: 'repeat(auto-fill, minmax(280px, 1fr))',
                gap: 16,
              }}
            >
              {collections.map((collection) => {
                const hasDocuments = collection.document_count > 0
                return (
                  <article
                    key={collection.id}
                    style={{
                      padding: 20,
                      background: t.surface,
                      border: `1px solid ${t.border}`,
                      borderRadius: 10,
                      display: 'flex',
                      flexDirection: 'column',
                      gap: 12,
                    }}
                  >
                    <div
                      style={{
                        width: 34,
                        height: 34,
                        borderRadius: 8,
                        background: t.accentSoft,
                        display: 'grid',
                        placeItems: 'center',
                      }}
                    >
                      <Icon name="sparkle" size={16} stroke={t.accent} />
                    </div>
                    <div>
                      <h2 style={{ margin: 0, fontSize: 15, fontWeight: 600 }}>
                        {collection.name}
                      </h2>
                      <p style={{ margin: '5px 0 0', color: t.textMuted, fontSize: 12 }}>
                        {collection.document_count} 份資料
                      </p>
                    </div>
                    <button
                      type="button"
                      disabled={!hasDocuments}
                      title={!hasDocuments ? '這個知識庫目前沒有資料，請先上傳並完成索引' : ''}
                      onClick={() => navigate(`/c/${collection.id}?studio=1`)}
                      style={{
                        marginTop: 'auto',
                        padding: '8px 12px',
                        borderRadius: 7,
                        border: `1px solid ${hasDocuments ? t.accent : t.border}`,
                        background: hasDocuments ? t.accent : t.surface2,
                        color: hasDocuments ? '#fff' : t.textSubtle,
                        cursor: hasDocuments ? 'pointer' : 'not-allowed',
                      }}
                    >
                      {hasDocuments ? '開啟產出工具' : '請先加入資料'}
                    </button>
                  </article>
                )
              })}
            </div>
          )}
        </div>
      </main>
    </div>
  )
}
