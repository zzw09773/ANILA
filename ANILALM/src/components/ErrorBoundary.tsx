import { Component, type ErrorInfo, type ReactNode } from 'react'

// Top-level error boundary. Renders a fallback UI on render-time errors
// and — critically — logs the React component stack so production crashes
// can be traced back to the offending component name (the JS stack is
// minified and full of react-dom internals; the component stack carries
// the SOURCE-LEVEL display names, which is what we actually need).

interface State {
  error: Error | null
  componentStack: string | null
}

interface Props {
  children: ReactNode
}

export class ErrorBoundary extends Component<Props, State> {
  state: State = { error: null, componentStack: null }

  static getDerivedStateFromError(error: Error): Partial<State> {
    return { error }
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    console.error('[ANILA LM crash]', error)
    console.error('[ANILA LM component stack]', info.componentStack)
    this.setState({ componentStack: info.componentStack ?? null })
  }

  reset = () => this.setState({ error: null, componentStack: null })

  render() {
    if (this.state.error) {
      return (
        <div
          style={{
            minHeight: '100vh',
            padding: 32,
            background: '#0B0D10',
            color: '#E8EAED',
            fontFamily: 'ui-monospace, monospace',
            display: 'flex',
            alignItems: 'flex-start',
            justifyContent: 'center',
          }}
        >
          <div style={{ maxWidth: 720, width: '100%' }}>
            <h1 style={{ fontSize: 18, color: '#FF6B6B', marginTop: 0 }}>
              [crash] {this.state.error.name}: {this.state.error.message}
            </h1>
            <pre
              style={{
                background: '#13161B',
                padding: 14,
                borderRadius: 8,
                fontSize: 12,
                lineHeight: 1.6,
                overflow: 'auto',
                color: '#9AA3AE',
              }}
            >
              {this.state.error.stack}
            </pre>
            {this.state.componentStack && (
              <>
                <div style={{ fontSize: 11, color: '#6B7280', marginTop: 14 }}>
                  Component stack:
                </div>
                <pre
                  style={{
                    background: '#13161B',
                    padding: 14,
                    borderRadius: 8,
                    fontSize: 11,
                    lineHeight: 1.55,
                    overflow: 'auto',
                    color: '#7C7BFF',
                  }}
                >
                  {this.state.componentStack}
                </pre>
              </>
            )}
            <button
              onClick={() => {
                localStorage.clear()
                window.location.replace('/anilalm/')
              }}
              style={{
                marginTop: 14,
                padding: '8px 14px',
                borderRadius: 8,
                border: '1px solid #7C7BFF',
                background: 'rgba(124,123,255,0.12)',
                color: '#7C7BFF',
                cursor: 'pointer',
                fontFamily: 'inherit',
                fontSize: 13,
                marginRight: 8,
              }}
            >
              [ ↻ 清除 localStorage 並重新載入 ]
            </button>
            <button
              onClick={this.reset}
              style={{
                marginTop: 14,
                padding: '8px 14px',
                borderRadius: 8,
                border: '1px solid #262B34',
                background: '#13161B',
                color: '#E8EAED',
                cursor: 'pointer',
                fontFamily: 'inherit',
                fontSize: 13,
              }}
            >
              [ retry ]
            </button>
          </div>
        </div>
      )
    }
    return this.props.children
  }
}
