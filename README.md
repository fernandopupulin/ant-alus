# @nt@lus

Seletor de regressão para o desafio Blast Radius, implementado a partir do protocolo do avaliador e do SQLGlot na revisão fixada.

O build executa a suíte original e mede chamadas de funções, leituras de arquivos e testes que iniciam subprocessos. Durante a coleta, as tarefas do pool são executadas no processo que registra a cobertura, preservando a serialização dos argumentos e dos resultados. O seletor aplica cada diff isoladamente e usa essas observações para localizar candidatos. Mudanças que alteram declarações de SQLGlot ou diffs que não puderam ser aplicados seguem a suíte inteira. Um processo isolado pode confirmar a passagem de candidatos de custo baixo. Os testes que ficaram sem veredito continuam na seleção.

O escalonamento compara de um a oito workers em milissegundos. O executável lê apenas `tests.json` e os diffs de `/data`. Resultados esperados da amostra ficam com o avaliador.

```text
docker build --platform linux/amd64 -t ant-alus .
```

Para executar a amostra, monte `sample/commits` em `/data/commits` e `sample/tests.json` em `/data/tests.json`, conforme o kit oficial. A opção `ANTALUS_STATIC=1` permite medir a seleção antes da verificação dinâmica.
