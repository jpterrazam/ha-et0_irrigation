# ET0 Irrigation - Especificacao Tecnica V2

Status: aprovado para implementacao
Data de alinhamento: 2026-04-01
Escopo: requisitos minimos funcionais

## 1. Objetivo
Automatizar a irrigacao do jardim com base na necessidade hidrica real de cada zona, considerando:
- ET0 global (Penman-Monteith)
- chuva global
- rega efetiva por zona

A decisao de rega ocorre uma vez por dia no horario configurado.

## 2. Modelo global

### 2.1 ET0 global
- Calculo periodico a cada 30 minutos.
- Entradas obrigatorias: temperatura, umidade, radiacao solar, vento, pressao atmosferica, latitude e altitude.
- ET0 global acumulada no dia (mm).
- Reinicio diario as 00:00.

### 2.2 Deficit global de referencia
- Atualizacao a cada 30 minutos.
- Formula de referencia: deficit_global = ET0_global_acumulada - chuva_global_acumulada.
- Uso principal: referencia diagnostica e monitoramento.
- Reinicio diario as 00:00.

### 2.3 Configuracoes globais
- Horario diario de verificacao da irrigacao.
- Deficit minimo para irrigar (mm).

## 3. Modelo por zona
Cada zona representa uma valvula independente.

### 3.1 Parametros por zona
- Fator de exposicao solar: 0 a 200%.
- Vazao de rega: mm/min.
- Dias maximos sem irrigacao: 0 a 5.
- Tempo minimo de rega: minutos.
- Tempo maximo de rega: minutos (padrao 30).
- Zonas companheiras: lista de zonas que devem operar junto.

### 3.2 Restricoes de configuracao
- Nao e permitido relacionamento de companheira em cadeia.
  - Exemplo invalido: A exige B e B exige C.
- Zona companheira deve estar no mesmo grupo de execucao.
  - Configuracao em grupos diferentes deve ser invalida.

## 4. Deficit individual por zona

### 4.1 Atualizacao
- Atualiza a cada 30 minutos.
- Deve ocorrer apos o calculo global no mesmo ciclo.

### 4.2 Formula conceitual
- deficit_zona = deficit_zona_anterior
  + ET0_global_ajustada_pela_exposicao
  - chuva_global
  - rega_efetiva_da_zona

Onde:
- ET0_global_ajustada_pela_exposicao = ET0_global_periodo * fator_exposicao_zona.
- chuva_global e a mesma para todas as zonas.
- rega_efetiva_da_zona = tempo_valvula_aberta * vazao_zona.

### 4.3 Persistencia
- O deficit da zona nao reinicia diariamente.
- O valor acumula entre dias ate compensacao por chuva e/ou rega.
- Valor negativo e permitido (superavit).

## 5. Dias sem irrigacao (por zona)

### 5.1 Contagem
- Contador independente por zona.
- Baseado em dias corridos desde a ultima condicao de rega efetiva.

### 5.2 O que conta como rega efetiva
A contagem deve ser reiniciada quando ocorrer qualquer uma das condicoes abaixo:
- houve irrigacao efetiva da propria zona (valvula aberta com aplicacao real de agua), ou
- a chuva reduziu o deficit da zona para zero ou para valor negativo (superavit).

## 6. Grupos de zonas e ordem de execucao

### 6.1 Agrupamento
- Zonas podem ser organizadas em grupos para execucao simultanea.

### 6.2 Ordem
- O sistema processa um grupo por vez.
- Dentro do grupo, todas as zonas elegiveis iniciam juntas.
- Cada zona pode encerrar em tempo diferente.
- O proximo grupo inicia somente apos todas as zonas do grupo atual encerrarem.

## 7. Logica diaria de acionamento

### 7.1 Momento de avaliacao
- Executar uma vez por dia no horario global configurado.
- Nao aplicar bloqueio global por regra de "choveu o suficiente ontem".
- A decisao deve ser por zona, baseada nas regras das secoes 7.3 e 7.4.

### 7.2 Tempo teorico por zona
- tempo_teorico_min = deficit_zona_mm / vazao_mm_min.

### 7.3 Regra principal por deficit minimo
- Se deficit_zona < deficit_minimo_global, tempo da zona = 0.
- Se deficit_zona >= deficit_minimo_global, zona elegivel para irrigacao por deficit.

### 7.4 Regra de dias maximos sem irrigacao
- Ao atingir dias_maximos_sem_irrigacao da zona, irrigacao deve ser forcada,
  mesmo que deficit_zona < deficit_minimo_global.
- Em irrigacao forcada, aplicar pelo menos tempo_minimo.

### 7.5 Composicao do tempo final
Para zona elegivel por deficit ou por forca de dias:
- tempo_base =
  - tempo_teorico_min, para elegibilidade por deficit;
  - tempo_minimo, para elegibilidade apenas por dias maximos.
- tempo_final = max(tempo_base, tempo_minimo).
- Aplicar limite superior: tempo_final <= tempo_maximo,
  exceto na regra de excecao de companheira (secao 8.3).

### 7.6 Precisao de execucao
- Precisao final em segundos.
- Conversao recomendada: segundos = round(tempo_final_min * 60).

## 8. Sincronizacao de zonas companheiras

### 8.1 Regra de simultaneidade
- Se zona A exige zona B como companheira:
  - A e B devem permanecer ligadas simultaneamente durante todo o tempo exigido por A.

### 8.2 Ajuste de tempo da companheira
- Se tempo de B for menor que tempo de A, elevar tempo de B para no minimo tempo de A.

### 8.3 Excecao ao tempo maximo
- Se a sincronizacao exigir que B ultrapasse seu tempo_maximo,
  essa ultrapassagem e permitida somente nesse caso.
- Fora desse caso, tempo_maximo continua obrigatorio.

## 9. Validacoes obrigatorias de configuracao
As validacoes abaixo devem bloquear configuracao invalida:
- Companheira em cadeia detectada.
- Zona companheira fora do mesmo grupo.
- Vazao <= 0.
- Fator de exposicao fora de 0 a 200%.
- Dias maximos fora de 0 a 5.
- Tempo minimo < 0.
- Tempo maximo <= 0.

## 10. Criterios de aceite minimos

1. ET0 global atualiza a cada 30 min e reinicia as 00:00.
2. Deficit global atualiza a cada 30 min e reinicia as 00:00.
3. Deficit por zona atualiza a cada 30 min, apos calculo global, e nao reinicia as 00:00.
4. Chuva global e aplicada igualmente em todas as zonas no calculo de deficit.
5. Zona com deficit abaixo do minimo recebe tempo 0, salvo forca por dias maximos.
6. Forca por dias maximos gera irrigacao minima da zona.
7. Tempo final respeita minimo e maximo por zona, exceto excecao de companheira.
8. Quando A exige B, B nao pode desligar antes de A.
9. Ultrapassar tempo maximo so e valido para sincronizacao de companheira.
10. Grupos executam em sequencia; zonas do grupo executam em paralelo com encerramento individual.
11. Companheira em cadeia e rejeitada na configuracao.
12. Companheira em grupo diferente e rejeitada na configuracao.
13. Dias sem irrigacao reiniciam com irrigacao efetiva ou chuva que zera/supera o deficit.
14. Execucao final de tempo ocorre com precisao de segundos.

## 11. Fora de escopo desta versao
- Otimizacao energetica/pico de consumo eletrico.
- Previsao meteorologica futura para antecipacao de rega.
- Controle por prioridade agronomica avancada entre grupos.

## 12. Diretriz para refatoracao
A refatoracao deve preservar comportamento funcional desta especificacao.
Qualquer divergencia deve ser tratada como alteracao de requisito e aprovada antes da implementacao.

## 13. Funcionalidades complementares aprovadas

### 13.1 Mantidas no produto
- Pressao atmosferica como entrada obrigatoria do ET0.
- Piso dinamico de superavit (limite de deficit negativo por zona).
- Fallback de entrada invalida para ultimo valor valido no ET0.
- Servico de ajuste dinamico de parametros por zona.
- Servico de limpeza de automacoes fantasma.
- Botao de reset manual de deficit de todas as zonas.
- Geracao automatica de dashboard Lovelace.
- Flag reconfigure_layout no options flow.

### 13.2 Removidas da regra alvo
- Bloqueio global da irrigacao quando choveu o suficiente no dia anterior.
- Compatibilidade legada com tipo de zona fixed.

### 13.3 Notas de evolucao
- A geracao automatica de dashboard permanece ativa e sera aperfeicoada em versoes futuras.
