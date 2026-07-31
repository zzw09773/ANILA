import client from './client'

export const listDepartments = () =>
  client.get('/api/departments')

// 巢狀樹（院部 → 研究所／中心 → 組／科）。後端已排序並且能容忍環狀資料，
// 所以畫面上的層級順序以這支為準，列上的明細再從扁平清單補。
export const getDepartmentTree = () =>
  client.get('/api/departments/tree')

export const createDepartment = (data) =>
  client.post('/api/departments', data)

export const updateDepartment = (id, data) =>
  client.put(`/api/departments/${id}`, data)

export const deactivateDepartment = (id) =>
  client.delete(`/api/departments/${id}`)
