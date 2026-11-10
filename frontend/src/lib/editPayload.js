// Nota a enviar na edição. Com vários lançamentos selecionados o campo começa vazio,
// e mandar esse vazio apagaria a nota de todos. Campo intocado vai como null, que a
// API trata como "não mexer". Campo apagado de propósito vai como string vazia.
export function noteToSave(note, touched) {
  return touched ? note.trim() : null
}
