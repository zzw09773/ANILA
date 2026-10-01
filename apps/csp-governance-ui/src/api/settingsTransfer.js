import client from './client'

export const exportSettings = () => client.get('/api/admin/settings-export')

export const importSettings = (document, { dryRun }) =>
  client.post('/api/admin/settings-import', { dry_run: dryRun, document })
