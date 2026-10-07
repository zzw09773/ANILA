import client from './client'

export const getHrDatabase = () => client.get('/api/admin/hr-database')

export const updateHrDatabase = (body) => client.put('/api/admin/hr-database', body)

export const testHrDatabase = (employeeNo) =>
  client.post('/api/admin/hr-database/test', { employee_no: employeeNo })
