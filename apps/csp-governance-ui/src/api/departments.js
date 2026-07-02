import client from './client'

export const listDepartments = () =>
  client.get('/api/departments')

export const createDepartment = (data) =>
  client.post('/api/departments', data)

export const updateDepartment = (id, data) =>
  client.put(`/api/departments/${id}`, data)

export const deactivateDepartment = (id) =>
  client.delete(`/api/departments/${id}`)
