export function createPollGuard() {
  let latestGen = 0
  let activeGen = 0

  return {
    begin({ skipBusy = false } = {}) {
      if (skipBusy && activeGen) return null
      const gen = ++latestGen
      activeGen = gen
      return {
        gen,
        isCurrent: () => gen === latestGen,
      }
    },
    end(gen) {
      if (activeGen === gen) activeGen = 0
    },
  }
}