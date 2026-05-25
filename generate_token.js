const { generate } = require('youtube-po-token-generator');

generate().then(r => {
    console.log(JSON.stringify({
        visitor_data: r.visitorData,
        po_token: r.poToken
    }));
}).catch(e => {
    process.stderr.write(e.message + '\n');
    process.exit(1);
});