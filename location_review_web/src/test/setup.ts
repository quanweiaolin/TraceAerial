import '@testing-library/jest-dom'

// jsdom没有实现URL.createObjectURL/revokeObjectURL(Blob场景)——
// ImageGallery/apiFetchBlob在测试里真的会调用到这两个API，不polyfill
// 会直接抛"Not implemented"，不是业务代码的锅。
if (typeof URL.createObjectURL !== 'function') {
  URL.createObjectURL = () => `blob:mock-${Math.random().toString(36).slice(2)}`
}
if (typeof URL.revokeObjectURL !== 'function') {
  URL.revokeObjectURL = () => {}
}
