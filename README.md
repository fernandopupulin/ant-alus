# @nt@lus

Seletor de regressão para o desafio Blast Radius, implementado a partir do protocolo do avaliador e do SQLGlot na revisão fixada.

O build executa a suíte original e mede chamadas de funções, instâncias de classes de SQLGlot, leituras de arquivos e testes que iniciam subprocessos. Durante a coleta, as tarefas do pool são executadas no processo que registra a cobertura, preservando a serialização dos argumentos e dos resultados. O seletor aplica cada diff isoladamente e usa essas observações para localizar candidatos. Para configurações de classe reconhecidas, ele usa o mapa de instâncias, preserva leitores diretos e mantém testes com cobertura inconclusiva. Declarações desconhecidas ou diffs que não puderam ser aplicados seguem a suíte inteira. Um processo isolado pode confirmar a passagem de candidatos de custo baixo. Os testes que ficaram sem veredito continuam na seleção.

O escalonamento compara de um a oito workers em milissegundos. O executável lê apenas `tests.json` e os diffs de `/data`. Resultados esperados da amostra ficam com o avaliador.

```text
docker build --platform linux/amd64 -t ant-alus .
```

Para executar a amostra, monte `sample/commits` em `/data/commits` e `sample/tests.json` em `/data/tests.json`, conforme o kit oficial. O seletor tenta uma única execução local por diff de código quando pode dispensar todos os workers desse diff com uma checagem barata. O lote de verificação tem no máximo seis testes e a janela de reexecução dura até seis segundos no total. Use `ANTALUS_STATIC=1` para medir apenas a seleção por cobertura.
