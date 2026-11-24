# FlowCaptions

Programa para Windows que baixa vídeos (YouTube, X, Instagram, Twitch, Kick...), gera legenda em português, acha trechos em lives longas, transcreve e traduz `.srt`.

## Instalar (primeira vez)
1. Abra a página **Releases** deste repositório e baixe o `FlowCaptions.zip` da versão mais recente (poucos MB).
2. Extraia a pasta `FlowCaptions` (de preferência em `C:\FlowCaptions`, sem acentos no caminho).
3. Dê dois cliques em `FlowCaptions.bat`. Na primeira vez ele baixa e instala sozinho o Python, as bibliotecas e o ffmpeg (precisa de internet; com placa NVIDIA são uns 2 GB). Quando terminar, abra o `FlowCaptions.bat` de novo.
4. Na primeira abertura o programa mostra como criar a sua chave do Gemini (grátis) e, se quiser, exportar os seus cookies.

## Atualizações
O programa verifica sozinho ao abrir. Quando houver versão nova aparece **"Nova versão X · atualizar"** no canto de cima: um clique atualiza e reabre.

## Privacidade
A chave do Gemini e os cookies ficam só na pasta do programa, no seu computador. Nunca envie `env`, `config.json` nem `*cookies*.txt` a ninguém.
