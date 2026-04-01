# ET0 Irrigation - Custom Component for Home Assistant

Componente de irrigacao automatica por ET0 (Penman-Monteith FAO-56), com controle por zonas, execucao por grupos e deficit hidrico acumulado por zona.

## Visao geral

Objetivo:
- irrigar quando ha necessidade real de agua por zona;
- considerar ET0, chuva e rega efetiva;
- executar uma verificacao diaria no horario configurado.

Pontos-chave da versao atual:
- todas as zonas sao ET0 (modo fixed foi removido);
- decisao de irrigacao e por zona;
- nao existe bloqueio global por chuva do dia anterior;
- suporte a tempo minimo e tempo maximo por zona;
- forca de irrigacao por dias maximos sem rega;
- sincronizacao de zonas companheiras.
- latitude e obtida automaticamente pelo Home Assistant;
- altitude e informada manualmente no config flow.

## O que o componente cria

### Entidades globais

| Entidade | Funcao |
|---|---|
| `sensor.et0_irrigation_et0_today` | ET0 acumulada do dia atual (mm) |
| Sensor Water Deficit Nd (unique_id `et0_irrigation_deficit_Nd`) | Deficit global diario de referencia (ET0 hoje - chuva hoje), com atributos de diagnostico |

Observacao:
- N depende da configuracao interna de dias do sensor de deficit (padrao 5 no fluxo atual).

### Entidades por zona

Para cada zona configurada:

| Entidade | Funcao |
|---|---|
| `sensor.et0_irrigation_zone_deficit_<slug_da_zona>` | Deficit acumulado da zona (mm) |

### Botao

| Entidade | Funcao |
|---|---|
| Botao "Reset Zone Deficits" | Zera o deficit de todas as zonas |

## Instalacao

1. Copie a pasta `et0_irrigation` para `config/custom_components/`.
2. Reinicie o Home Assistant.
3. Va em Configuracoes -> Dispositivos e Servicos -> Adicionar Integracao.
4. Selecione ET0 Irrigation e finalize o config flow.

## Sensores meteorologicos necessarios

| Parametro | Unidade aceita |
|---|---|
| Temperatura | C ou F |
| Umidade relativa | % |
| Velocidade do vento | km/h ou m/s |
| Luminosidade | lux |
| Pressao atmosferica | hPa ou kPa |
| Chuva acumulada do dia | mm |

## Configuracao global

| Campo | Descricao |
|---|---|
| Horario da irrigacao | Hora de disparo da automacao gerada |
| Deficit minimo para irrigar | Limite global comparado com o deficit de cada zona |
| Altitude do local (m) | Usada no calculo ET0 como apoio ao parametro atmosferico; pode ser ajustada manualmente no config flow |

## Configuracao por zona

| Campo | Faixa | Descricao |
|---|---|---|
| Switch | - | Valvula da zona |
| Taxa de aplicacao (mm/min) | > 0 | Converte tempo ligado em mm aplicados |
| Fator ET0 | 0.1 a 2.0 | Ajuste de exposicao da zona sobre a ET0 |
| Tempo minimo (min) | 0 a 120 | Piso de tempo por acionamento |
| Tempo maximo (min) | 1 a 240 | Teto por ciclo (com excecao para sincronizacao de companheira) |
| Max. dias sem irrigacao | 0 a 5 | Forca irrigacao ao atingir o limite; 0 = nao pode ficar sem irrigacao |
| Requer companheira | bool | Indica que a zona precisa operar junto de outra zona |
| Pool de companheiras | lista | Zonas elegiveis para acompanhar a zona dependente |

Validacoes de configuracao:
- nao permite companheira em cadeia;
- exige companheira no mesmo grupo;
- valida limites de fator, tempos e dias.

## Como a automacao funciona

O componente gera/atualiza automaticamente uma automacao em `automations.yaml`.

Fluxo:
1. Dispara no horario configurado.
2. Calcula duracao de cada zona em segundos.
3. Processa um grupo por vez.
4. Liga zonas elegiveis do grupo simultaneamente.
5. Desliga cada zona no proprio tempo.
6. Inicia o proximo grupo apenas quando o grupo atual termina.

Regras de acionamento por zona:
1. Tempo teorico: deficit_zona / taxa_aplicacao.
2. Se deficit_zona < deficit_minimo, tempo = 0.
3. Excecao: se dias_sem_irrigacao >= max_dias_sem_irrigacao, forca irrigacao.
4. Tempo final aplica minimo e maximo configurados.
5. Precisao de execucao em segundos.

## Zonas companheiras

Quando uma zona depende de companheira:
- ambas devem permanecer ligadas durante o tempo da zona dependente;
- se necessario, o tempo da companheira e elevado para acompanhar;
- se esse ajuste ultrapassar o tempo maximo da companheira, a ultrapassagem e permitida somente nesse caso.

## Modelo de deficit por zona

Formula conceitual:

`deficit_zona = deficit_anterior + ET0_ajustada_zona - chuva_global - irrigacao_efetiva_zona`

Onde:
- ET0_ajustada_zona = ET0_global * fator_da_zona;
- chuva_global e aplicada igualmente a todas as zonas;
- irrigacao_efetiva_zona = minutos_valvula_aberta * taxa_aplicacao.

Comportamento:
- deficit por zona nao reinicia na virada do dia;
- pode ficar negativo (superavit);
- ha piso dinamico de superavit para evitar acumulacao negativa excessiva.

## Chuva que conta como "rega efetiva"

No fechamento de um dia, para cada zona:
- se chuva_dia >= ET0_ajustada_da_zona_dia, o dia conta como rega efetiva;
- isso reinicia o contador de dias sem irrigacao da zona.

## Servicos

### set_zone_parameter
Atualiza parametros de uma zona e recarrega a integracao.

Campos suportados:
- zone_switch (obrigatorio)
- factor
- min_minutes
- max_minutes
- application_rate
- max_days_without_irrigation

### cleanup_automation_ghosts
Remove registros antigos/duplicados de automacoes ET0 no runtime e no entity registry.

## Dashboard

O componente gera dashboard Lovelace automaticamente.

Notas:
- a geracao automatica segue ativa;
- o layout sera aperfeicoado em versoes futuras.

## Reset manual dos deficits

Ao acionar o botao "Reset Zone Deficits":
- todas as zonas voltam para `0.0 mm`;
- `last_processed_day` e ajustado para ontem;
- `last_effective_watering_day` e ajustado para hoje.

## Troubleshooting rapido

Zona nao irriga:
1. Verifique se o deficit da zona atingiu o minimo global, ou se a forca por dias foi acionada.
2. Verifique se a automacao gerada existe e esta habilitada.
3. Verifique se o switch da zona responde a ON/OFF.

Zona dependente nao liga:
1. Verifique se ha companheira valida no mesmo grupo.
2. Verifique configuracao de pool de companheiras.

Deficit de zona parado:
1. Verifique os sensores meteorologicos.
2. Verifique a entidade de ET0 Today.
3. Veja os atributos do sensor da zona (`environment_source`, `days_without_irrigation`, `last_irrigation_mm`).

## Referencia de requisitos

Para regras funcionais detalhadas e criterios de aceite, consulte `SPECIFICACAO_TECNICA_V2.md`.
