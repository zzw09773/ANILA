import client from './client'

export const getDiskMounts = () => client.get('/api/admin/disk-mounts')

export const getTlsCertificate = () => client.get('/api/admin/tls-certificate')
