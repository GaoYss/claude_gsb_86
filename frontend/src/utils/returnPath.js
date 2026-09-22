/**
 * 规范化下钻返回地址：只允许站内绝对路径（如 /hazards?status=closed），
 * 拒绝外部 URL 或协议相对地址，避免 return 参数被构造成跳转外链。
 */
export function safeReturnPath(value) {
  if (typeof value !== 'string') return ''
  // 反斜杠在部分浏览器会被规范化为 // 形成协议相对地址，一并拒绝
  if (value.startsWith('/') && !value.startsWith('//') && !value.startsWith('/\\')) return value
  return ''
}
