# Weekly HNI Bot V1.1 — AWS वर लावण्याच्या पायऱ्या

## पायरी 0 — आधी Dhan तपासा
- Dhan app → Positions: जुन्या 15-delta चे कोणतेही position उघडे नाही ना? असेल तर आधी हाताने बंद करा.
- Dhan app → Orders: pending orders नाहीत ना?

## पायरी 1 — जुना 15-delta bot पूर्ण बंद करा (EC2 वर SSH करून)
    ps aux | grep -i python            # कोणते python चालू आहे ते पाहा
    sudo systemctl list-units --type=service | grep -i -E "bot|algo|dhan|condor"
    sudo systemctl stop  <जुन्या_service_चे_नाव>
    sudo systemctl disable <जुन्या_service_चे_नाव>
    # service नसेल तर (nohup/screen ने चालवला असेल):
    pkill -f <जुन्या_script_चे_नाव>.py
    crontab -l                          # जुन्या bot च्या ओळी असतील तर: crontab -e ने काढा
    mv ~/<जुना_folder> ~/backup_15delta_$(date +%F)   # delete नाही, backup
    ps aux | grep -i python            # पुन्हा तपासा — जुने काहीही चालू नसावे

## पायरी 2 — नवीन files टाका
    mkdir -p ~/hni_bot && cd ~/hni_bot
    # config.py, hni_bot.py, requirements.txt, hni-bot.service, .env.example इथे upload करा (WinSCP / scp)
    cp .env.example .env && nano .env    # token, client id, telegram भरा
    python3 -m venv venv
    ./venv/bin/pip install -r requirements.txt

## पायरी 3 — आधी market बंद असताना तपासणी (order जात नाही)
    ./venv/bin/python -c "import hni_bot,config;print('OK',config.VERSION)"
    ./venv/bin/python hni_bot.py        # Telegram वर "🤖 HNI Bot V1.1 सुरू" आला पाहिजे
    # Telegram वर /preview पाठवा → strikes, margin/set, sets दिसले पाहिजेत (order नाही)
    # Ctrl+C ने थांबवा

## पायरी 4 — कायमचा चालू ठेवा
    sudo cp hni-bot.service /etc/systemd/system/
    sudo systemctl daemon-reload
    sudo systemctl enable --now hni-bot
    sudo systemctl status hni-bot
    tail -f ~/hni_bot/hni_bot.log      # log पाहण्यासाठी

## Telegram commands
/status  /preview  /pause  /resume  /exitnow  /help

## नंतर लागणारे
- Token बदलला: nano .env → sudo systemctl restart hni-bot
- Settings बदलली (config.py): sudo systemctl restart hni-bot
- Trades ची यादी: ~/hni_bot/hni_trades.csv
- चालू trade ची माहिती: ~/hni_bot/hni_state.json (हाताने बदलू नका)
