# RBK Vendedor IA API v0.9.3

A busca local passa a reproduzir o modo **palavras-chave** do Olist.

## Regra

- todas as palavras-base precisam aparecer em qualquer posição da descrição;
- a comparação é por substring;
- `160` também encontra `FS160`, `GX160` e `1600`, como no ERP;
- características restantes são usadas para relevância;
- relevância vem antes de preço e estoque;
- preço e estoque ordenam produtos com a mesma correspondência.

## Exemplos de palavras-base

- cinto de sustentação universal laranja → `cinto sustentacao`;
- luva de malha pigmentada branca → `luva malha`;
- embreagem para MS 382 → `embreagem 382`;
- sabre para MS 170 → `sabre 170`;
- pistão para FS 160 → `pistao 160`;
- carburador 43cc → `carburador 43cc`.

O prefixo MS/FS não é obrigatório, pois os cadastros podem usar ST, apenas
o número ou outras abreviações.
