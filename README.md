# @nt@lus

Seletor de regressão para o desafio Blast Radius, implementado a partir do protocolo do avaliador e do SQLGlot na revisão fixada.

O build executa a suíte do SQLGlot fixado e registra chamadas, leituras de arquivos e uso de subprocessos. A coleta rastreia também linhas de condições dos parsers, comparações de identidade e igualdade, acessos a entradas de escapes numéricos e leituras de `WEEK_OFFSET`. Cada diff é analisado isoladamente. O seletor usa a observação mais específica que consegue justificar, conserva dependências inconclusivas e recorre à suíte inteira quando a aplicação do diff ou a análise falha.

O escalonamento compara de um a oito workers em milissegundos. O executável lê apenas `tests.json` e os diffs de `/data`. Resultados esperados da amostra ficam com o avaliador.

```text
docker build --platform linux/amd64 -t ant-alus .
```

Para executar a amostra, monte `sample/commits` em `/data/commits` e `sample/tests.json` em `/data/tests.json`, conforme o kit oficial. Um diff com poucos candidatos baratos pode receber uma checagem isolada de até 2,5 s. Só são dispensados os testes que completam a checagem com sucesso. Use `ANTALUS_STATIC=1` para medir apenas a seleção por cobertura.
