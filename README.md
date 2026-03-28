# ET0 Irrigation - Custom Component for Home Assistant

Componente de irrigacao automatica baseado em ET0 (Penman-Monteith FAO-56),
com controle por zonas, blocos simultaneos e deficit hidrico por zona.

## O que o componente cria

### Entidades globais

| Entidade | Funcao |
|---|---|
| `sensor.et0_irrigation_et0_today` | ET0 acumulada do dia atual (mm) |
| `sensor.et0_irrigation_deficit_1d` | Deficit ambiental do ultimo dia fechado (mm) |

### Entidades por zona

Para cada zona configurada:

| Entidade | Funcao |
|---|---|
| `sensor.et0_irrigation_zone_deficit_<slug_da_zona>` | Deficit acumulado da zona (mm) |

### Botao de servico

| Entidade | Funcao |
|---|---|
| `button.reset_zone_deficits` (nome exibido: Reset Zone Deficits) | Zera o deficit de todas as zonas |

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

## Configuracao principal

| Campo | Descricao |
|---|---|
| Horario da irrigacao | Hora de disparo da automacao gerada |
| Deficit minimo para irrigar | Limite usado por todas as zonas para decidir ligar |

## Configuracao por zona

| Campo | Descricao |
|---|---|
| Tipo | `et0` (dinamico) ou `fixed` (tempo fixo) |
| Switch | Valvula da zona |
| Taxa de aplicacao (mm/min) | Conversao de tempo ligado para mm aplicados |
| Fator de ajuste ET0 | Multiplica ET0 ambiental para adequar microclima da zona |
| Maximo de dias sem irrigacao | Usado para dimensionar o limite inferior (floor) de superavit |

## Como a automacao funciona

O componente gera e atualiza automaticamente uma automacao em `automations.yaml`.

Fluxo:
1. Dispara no horario configurado.
2. Calcula a duracao de cada zona.
3. Liga zonas por bloco (simultaneas no bloco).
4. Desliga cada zona no proprio tempo.

Regra de irrigacao por zona:
- So irriga quando `zone_deficit >= deficit_minimo`.

Duracao por tipo:
- Zona `et0`: `tempo = max(deficit * fator / taxa_aplicacao, 1 minuto)`.
- Zona `fixed`: tempo fixo configurado.

## Modelo de deficit por zona

A logica operacional segue:

`deficit = deficit_anterior + (ET0_ambiente * fator) - chuva_efetiva - irrigacao_efetiva`

Onde:
- ET0_ambiente vem do baseline global do componente (preferencia pelo sensor 1d da integracao).
- irrigacao_efetiva vem do evento OFF do switch da zona:
  `mm_irrigados = minutos_ligado * taxa_aplicacao`.

## Limite inferior de superavit (floor dinamico)

Para evitar carregar superavit muito negativo por muito tempo:

`deficit = max(deficit, limite_inferior)`

Com:

`limite_inferior = -(ET0_medio_ambiente * fator_da_zona * max_dias_sem_irrigacao)`

Detalhes importantes:
- ET0 medio usa historico global de 7 dias (`et0_daily_history`).
- Esse historico e alimentado com ET0 de dia fechado (na atualizacao do deficit 1d),
  nao com os valores parciais intra-dia do ET0 Today.
- Enquanto nao ha historico suficiente, usa fallback base de 5 mm.

## Chuva que conta como "dia irrigado"

Se em um dia fechado `chuva >= ET0` daquele dia, a zona marca esse dia como
rega efetiva para efeito de atributos diagnosticos.

## Reset manual dos deficits

Ao pressionar o botao Reset Zone Deficits:
- todas as zonas sao zeradas para `0.0 mm`;
- `last_processed_day` vira ontem;
- `last_effective_watering_day` vira hoje.

## Comportamento em zona removida e re-incluida

Cada zona recebe um marcador `created_at` no config flow.
Quando a zona e re-incluida, o sensor detecta marcador novo e reinicia em zero,
mesmo que exista estado antigo no recorder.

## Principais atributos do sensor de zona

Exemplo de atributos expostos em `sensor.et0_irrigation_zone_deficit_*`:

```json
{
  "zone_switch": "switch.zona_1",
  "zone_type": "et0",
  "zone_created_at": "2026-03-27T15:30:00",
  "et0_factor": 1.2,
  "application_rate_mm_min": 0.55,
  "min_surplus_floor_mm": -16.8,
  "et0_history_days": 7,
  "et0_average_basis": "historical",
  "last_processed_day": "2026-03-26",
  "last_effective_watering_day": "2026-03-26",
  "days_without_irrigation": 1,
  "environment_source": "water_deficit_1d_entity",
  "last_irrigation_mm": 8.5
}
```

## Formula ET0 (resumo)

- Metodo: Penman-Monteith FAO-56.
- Ra (radiacao extraterrestre): calculada dinamicamente por latitude e dia do ano.
- Irradiacao: integracao trapezoidal de luminosidade (lux -> W/m2 -> Wh/m2).
- G (fluxo de calor no solo): 0 no passo diario.

## Troubleshooting rapido

Zona nao irriga:
1. Verifique se `sensor.et0_irrigation_zone_deficit_* >= deficit_minimo`.
2. Verifique se a automacao gerada existe e esta habilitada.
3. Verifique se o switch da zona responde a ON/OFF.

Deficit de zona parado:
1. Verifique se os sensores meteorologicos estao validos.
2. Verifique `sensor.et0_irrigation_deficit_1d`.
3. Veja o atributo `environment_source` da zona.

Reset geral:
1. Pressione o botao Reset Zone Deficits.
2. Confirme que todos os `zone_deficit` voltaram para 0.
