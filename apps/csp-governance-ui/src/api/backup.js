import client from './client'

export const getBackupStatus = () => client.get('/api/admin/backup-status')
