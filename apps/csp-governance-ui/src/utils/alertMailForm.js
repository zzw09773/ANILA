/** 警報寄信表單。密碼只在這次要改的時候送出，畫面不回填。 */

export function mailSettingsForForm(raw) {
  const source = raw && typeof raw === 'object' ? raw : {}
  return {
    enabled: !!source.enabled,
    smtp_host: source.smtp_host || '',
    smtp_port: Number(source.smtp_port) || 587,
    security: source.security || 'starttls',
    username: source.username || '',
    password: '',
    has_password: !!source.has_password,
    clearPassword: false,
    from_address: source.from_address || '',
    recipients: source.recipients || '',
    last_error: source.last_error || '',
  }
}

export function mailSettingsSaveBody(draft) {
  const body = {
    enabled: !!draft.enabled,
    smtp_host: String(draft.smtp_host || '').trim(),
    smtp_port: Number(draft.smtp_port) || 587,
    security: draft.security || 'starttls',
    username: String(draft.username || '').trim(),
    from_address: String(draft.from_address || '').trim(),
    recipients: draft.recipients || '',
  }
  if (draft.clearPassword) body.clear_password = true
  else if (draft.password) body.password = draft.password
  return body
}
