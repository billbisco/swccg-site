import java.sql.*;
import java.util.*;

/** Read-only aggregate projection through the Play application's existing JDBC configuration. */
class PlayAggregate {
    public static void main(String[] args) {
        try {
            String host=System.getenv("DB_CONTAINER_NAME"), db=System.getenv("MYSQL_DATABASE"), user=System.getenv("MYSQL_USER"), pass=System.getenv("MYSQL_PASSWORD");
            if(host==null||db==null||user==null||pass==null||!host.matches("[a-zA-Z0-9_.:-]+")||!db.matches("[a-zA-Z0-9_-]+"))throw new Exception();
            try(Connection c=DriverManager.getConnection("jdbc:mysql://"+host+"/"+db+"?connectTimeout=3000&socketTimeout=5000",user,pass)) {
                c.setReadOnly(true);
                long now=System.currentTimeMillis(), estimate=scalar(c,"SELECT COALESCE(SUM(TABLE_ROWS),0) FROM information_schema.tables WHERE table_schema=DATABASE() AND table_name IN ('game_history','player')");
                if(estimate>100000)throw new Exception();
                StringBuilder out=new StringBuilder("{\"observed_at\":"+(now/1000)+",\"windows\":{");
                for(int days:new int[]{1,7,30}) {
                    long start=now-days*86400000L;
                    long games=count(c,"SELECT COUNT(*) FROM game_history WHERE end_date>=? AND end_date<?",start,now);
                    long incomplete=count(c,"SELECT COUNT(*) FROM game_history WHERE end_date>=? AND end_date<? AND (winner_id IS NULL OR loser_id IS NULL)",start,now);
                    long players=count(c,"SELECT COUNT(DISTINCT participant) FROM (SELECT winner_id AS participant FROM game_history WHERE end_date>=? AND end_date<? UNION ALL SELECT loser_id FROM game_history WHERE end_date>=? AND end_date<?) p WHERE participant IS NOT NULL",start,now,start,now);
                    long accounts=count(c,"SELECT COUNT(*) FROM player WHERE account_creation_date>=FROM_UNIXTIME(?/1000) AND account_creation_date<FROM_UNIXTIME(?/1000)",start,now);
                    if(days!=1)out.append(','); out.append('"').append(days).append("\":{\"games\":").append(games).append(",\"partial_ids\":").append(incomplete).append(",\"participants\":").append(players).append(",\"local_accounts\":").append(accounts).append('}');
                }
                out.append("}}");System.out.println(out);
            }
        } catch(Exception e) { System.err.println("Play aggregate unavailable");System.exit(1); }
    }
    static long scalar(Connection c,String sql)throws SQLException {return count(c,sql);}
    static long count(Connection c,String sql,long... values)throws SQLException {
        try(PreparedStatement p=c.prepareStatement(sql)){p.setQueryTimeout(3);for(int i=0;i<values.length;i++)p.setLong(i+1,values[i]);try(ResultSet r=p.executeQuery()){if(!r.next())throw new SQLException();return r.getLong(1);}}
    }
}
