import client from './client'

export const listUnitAdmins = () => client.get('/api/unit-admins')

export const unitAdminCounts = () => client.get('/api/unit-admins/counts')

export const createUnitAdmin = (data) => client.post('/api/unit-admins', data)

export const revokeUnitAdmin = (id) => client.delete(`/api/unit-admins/${id}`)
