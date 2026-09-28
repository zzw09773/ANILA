import client from './client'

export const getAlertMail = () => client.get('/api/alerts/mail')

export const updateAlertMail = (body) => client.put('/api/alerts/mail', body)

export const sendAlertTestMail = () => client.post('/api/alerts/mail/test')
